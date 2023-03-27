# MIT License
#
# Copyright (c) 2020- CNRS
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import itertools
import math
import random
from collections import defaultdict
from typing import Dict, Optional, Sequence, Union

import matplotlib.pyplot as plt
import numpy as np
import torch
from pyannote.core import Segment, SlidingWindow, SlidingWindowFeature
from pyannote.database.protocol.protocol import Scope, Subset
from pytorch_lightning.loggers import MLFlowLogger, TensorBoardLogger
from torch.utils.data._utils.collate import default_collate
from torchaudio.backend.common import AudioMetaData
from torchmetrics import Metric
from torchmetrics.classification import BinaryAUROC, MulticlassAUROC, MultilabelAUROC

from pyannote.audio.core.task import Problem
from pyannote.audio.utils.powerset import Powerset
from pyannote.audio.utils.random import create_rng_for_worker

Subsets = list(Subset.__args__)
Scopes = list(Scope.__args__)


class SegmentationTaskMixin:
    """Methods common to most segmentation tasks"""

    def get_file(self, file_id):

        file = dict()

        file["audio"] = str(self.audios[file_id], encoding="utf-8")

        _audio_info = self.audio_infos[file_id]
        _encoding = self.audio_encodings[file_id]

        sample_rate = _audio_info["sample_rate"]
        num_frames = _audio_info["num_frames"]
        num_channels = _audio_info["num_channels"]
        bits_per_sample = _audio_info["bits_per_sample"]
        encoding = str(_encoding, encoding="utf-8")
        file["torchaudio.info"] = AudioMetaData(
            sample_rate=sample_rate,
            num_frames=num_frames,
            num_channels=num_channels,
            bits_per_sample=bits_per_sample,
            encoding=encoding,
        )

        return file

    def setup(self, stage: Optional[str] = None):
        """Setup method

        Parameters
        ----------
        stage : {'fit', 'validate', 'test'}, optional
            Setup stage. Defaults to 'fit'.
        """

        # duration of training chunks
        # TODO: handle variable duration case
        duration = getattr(self, "duration", 0.0)

        # list of possible values for each metadata key
        metadata_unique_values = defaultdict(list)
        metadata_unique_values["scope"] = Scopes
        metadata_unique_values["subset"] = Subsets

        audios = list()  # list of path to audio files
        audio_infos = list()
        audio_encodings = list()
        metadata = list()  # list of metadata

        annotated_duration = list()  # total duration of annotated regions (per file)
        annotated_regions = list()  # annotated regions
        annotations = list()  # actual annotations

        unique_labels = list()

        if self.has_validation:
            files_iter = itertools.chain(
                self.protocol.train(), self.protocol.development()
            )
        else:
            files_iter = self.protocol.train()

        for file_id, file in enumerate(files_iter):

            metadatum = dict()
            for key, value in file.items():

                if key in [
                    "audio",
                    "torchaudio.info",
                    "annotation",
                    "annotated",
                    "uri",
                    "classes",
                ]:
                    continue

                if key == "subset":
                    metadatum[key] = Subsets.index(value)

                elif key == "scope":
                    metadatum[key] = Scopes.index(value)
                    scope = value

                elif isinstance(value, str):
                    if value not in metadata_unique_values[key]:
                        metadata_unique_values[key].append(value)
                    metadatum[key] = metadata_unique_values[key].index(value)

                elif isinstance(value, int):
                    metadatum[key] = value

                elif isinstance(value, float):
                    print(f"Skipping float metadata value '{key}'.")
                    continue

                else:
                    raise NotImplementedError(
                        f"Metadata key '{key}' of type '{type(value)}' is not supported."
                    )

            metadata.append(tuple(metadatum[key] for key in metadata_unique_values))

            database_unique_labels = list()

            # reset list of file-scoped labels
            file_unique_labels = list()

            # path to audio file
            audios.append(str(file["audio"]))

            # audio info
            audio_info = file["torchaudio.info"]
            audio_infos.append(
                (
                    audio_info.sample_rate,  # sample rate
                    audio_info.num_frames,  # number of frames
                    audio_info.num_channels,  # number of channels
                    audio_info.bits_per_sample,  # bits per sample
                )
            )
            audio_encodings.append(audio_info.encoding)  # encoding

            # annotated regions and duration
            _annotated_duration = 0.0
            for segment in file["annotated"]:

                # skip annotated regions that are shorter than training chunk duration
                if segment.duration < duration:
                    continue

                # append annotated region
                annotated_region = (
                    file_id,
                    segment.duration,
                    segment.start,
                    segment.end,
                )
                annotated_regions.append(annotated_region)

                # increment annotated duration
                _annotated_duration += segment.duration

            # append annotated duration
            annotated_duration.append(_annotated_duration)

            # annotations
            for segment, _, label in file["annotation"].itertracks(yield_label=True):

                # update list of file-scope labels
                if label not in file_unique_labels:
                    file_unique_labels.append(label)
                # and convert label to its (file-scope) index
                file_label_idx = file_unique_labels.index(label)

                if scope == "database":
                    # update list of database-scope labels
                    if label not in database_unique_labels:
                        database_unique_labels.append(label)
                    # and convert label to its (database-scope) index
                    database_label_idx = database_unique_labels.index(label)
                else:
                    # mark current annotation as out-of-database-scope
                    database_label_idx = -1

                if scope == "global":
                    # update list of global-scope labels
                    if label not in unique_labels:
                        unique_labels.append(label)
                    # and convert label to its (global-scope) index
                    global_label_idx = unique_labels.index(label)
                else:
                    # mark current annotation as out-of-global-scope
                    global_label_idx = -1

                annotations.append(
                    (
                        file_id,  # index of file
                        segment.start,  # start time
                        segment.end,  # end time
                        file_label_idx,  # file-scope label index
                        database_label_idx,  # database-scope label index
                        global_label_idx,  # global-scope index
                    )
                )

        dtype = [(key, "i") for key in metadata_unique_values]
        self.metadata = np.array(metadata, dtype=dtype)

        # # turn list of databases into a numpy array
        # self.databases = np.array(databases, dtype=np.string_)

        # NOTE: read with str(self.audios[file_id], encoding='utf-8')
        self.audios = np.array(audios, dtype=np.string_)

        # turn list of files metadata into a single numpy array
        # TODO: improve using https://github.com/pytorch/pytorch/issues/13246#issuecomment-617140519

        dtype = [
            ("sample_rate", "i"),
            ("num_frames", "i"),
            ("num_channels", "i"),
            ("bits_per_sample", "i"),
        ]
        self.audio_infos = np.array(audio_infos, dtype=dtype)
        self.audio_encodings = np.array(audio_encodings, dtype=np.string_)

        self.annotated_duration = np.array(annotated_duration)

        # turn list of annotated regions into a single numpy array
        dtype = [("file_id", "i"), ("duration", "f"), ("start", "f"), ("end", "f")]
        self.annotated_regions = np.array(annotated_regions, dtype=dtype)

        # turn list of annotations into a single numpy array
        dtype = [
            ("file_id", "i"),
            ("start", "f"),
            ("end", "f"),
            ("file_label_idx", "i"),
            ("database_label_idx", "i"),
            ("global_label_idx", "i"),
        ]
        self.annotations = np.array(annotations, dtype=dtype)

        self.metadata_unique_values = metadata_unique_values

        if not self.has_validation:
            return

        validation_chunks = list()

        # obtain indexes of files in the validation subset
        validation_file_ids = np.where(
            self.metadata["subset"] == Subsets.index("development")
        )[0]

        # iterate over files in the validation subset
        for file_id in validation_file_ids:

            # get annotated regions in file
            annotated_regions = self.annotated_regions[
                self.annotated_regions["file_id"] == file_id
            ]

            # iterate over annotated regions
            for annotated_region in annotated_regions:

                # number of chunks in annotated region
                num_chunks = round(annotated_region["duration"] // duration)

                # iterate over chunks
                for c in range(num_chunks):
                    start_time = annotated_region["start"] + c * duration
                    validation_chunks.append((file_id, start_time, duration))

        dtype = [("file_id", "i"), ("start", "f"), ("duration", "f")]
        self.validation_chunks = np.array(validation_chunks, dtype=dtype)

    def setup_loss_func(self):
        if self.specifications.powerset:
            self.model.powerset = Powerset(
                len(self.specifications.classes),
                self.specifications.powerset_max_classes,
            )

    def default_metric(
        self,
    ) -> Union[Metric, Sequence[Metric], Dict[str, Metric]]:
        """Returns macro-average of the area under the ROC curve"""

        num_classes = len(self.specifications.classes)
        if self.specifications.problem == Problem.BINARY_CLASSIFICATION:
            return BinaryAUROC(compute_on_cpu=True)
        elif self.specifications.problem == Problem.MULTI_LABEL_CLASSIFICATION:
            return MultilabelAUROC(num_classes, average="macro", compute_on_cpu=True)
        elif self.specifications.problem == Problem.MONO_LABEL_CLASSIFICATION:
            return MulticlassAUROC(num_classes, average="macro", compute_on_cpu=True)
        else:
            raise RuntimeError(
                f"The {self.specifications.problem} problem type hasn't been given a default segmentation metric yet."
            )

    def adapt_y(self, collated_y: torch.Tensor) -> torch.Tensor:
        return collated_y

    def prepare_chunk(self, file_id: int, start_time: float, duration: float):
        """Prepare chunk

        Parameters
        ----------
        file_id : int
            File index
        start_time : float
            Chunk start time
        duration : float
            Chunk duration.

        Returns
        -------
        sample : dict
            Dictionary containing the chunk data with the following keys:
            - `X`: waveform
            - `y`: target
            - `meta`:
                - `scope`: target scope (0: file, 1: database, 2: global)
                - `database`: database index
                - `file`: file index
        """

        file = self.get_file(file_id)

        # read label scope
        scope = Scopes[self.metadata[file_id]["scope"]]
        label_idx = f"{scope}_label_idx"

        chunk = Segment(start_time, start_time + duration)

        sample = dict()
        sample["X"], _ = self.model.audio.crop(file, chunk, duration=duration)

        # use model introspection to predict how many frames it will output
        # TODO: this should be cached
        num_samples = sample["X"].shape[1]
        num_frames, _ = self.model.introspection(num_samples)
        resolution = duration / num_frames
        frames = SlidingWindow(start=0.0, duration=resolution, step=resolution)

        # gather all annotations of current file
        annotations = self.annotations[self.annotations["file_id"] == file_id]

        # gather all annotations with non-empty intersection with current chunk
        chunk_annotations = annotations[
            (annotations["start"] < chunk.end) & (annotations["end"] > chunk.start)
        ]

        # discretize chunk annotations at model output resolution
        start = np.maximum(chunk_annotations["start"], chunk.start) - chunk.start
        start_idx = np.floor(start / resolution).astype(np.int)
        end = np.minimum(chunk_annotations["end"], chunk.end) - chunk.start
        end_idx = np.ceil(end / resolution).astype(np.int)

        # get list and number of labels for current scope
        labels = np.unique(chunk_annotations[label_idx])
        num_labels = len(labels)

        # initial frame-level targets
        y = np.zeros((num_frames, num_labels), dtype=np.uint8)

        # map labels to indices
        mapping = {label: idx for idx, label in enumerate(labels)}

        for c, chunk_annotation in enumerate(chunk_annotations):
            start, end = start_idx[c], end_idx[c]
            label = mapping[chunk_annotation[label_idx]]
            y[start:end, label] = 1

        sample["y"] = SlidingWindowFeature(y, frames, labels=labels)

        metadata = self.metadata[file_id]
        sample["meta"] = {key: metadata[key] for key in metadata.dtype.names}
        sample["meta"]["file"] = file_id

        return sample

    def train__iter__helper(self, rng: random.Random, **filters):
        """Iterate over training samples with optional domain filtering

        Parameters
        ----------
        rng : random.Random
            Random number generator
        filters : dict, optional
            When provided (as {key: value} dict), filter training files so that
            only files such as file[key] == value are used for generating chunks.

        Yields
        ------
        chunk : dict
            Training chunks.
        """

        # indices of training files that matches domain filters
        training = self.metadata["subset"] == Subsets.index("train")
        for key, value in filters.items():
            training &= self.metadata[key] == value
        file_ids = np.where(training)[0]

        # turn annotated duration into a probability distribution
        annotated_duration = self.annotated_duration[file_ids]
        prob_annotated_duration = annotated_duration / np.sum(annotated_duration)

        duration = self.duration

        num_chunks_per_file = getattr(self, "num_chunks_per_file", 1)

        while True:

            # select one file at random (with probability proportional to its annotated duration)
            file_id = np.random.choice(file_ids, p=prob_annotated_duration)

            # generate `num_chunks_per_file` chunks from this file
            for _ in range(num_chunks_per_file):

                # find indices of annotated regions in this file
                annotated_region_indices = np.where(
                    self.annotated_regions["file_id"] == file_id
                )[0]

                # turn annotated regions duration into a probability distribution
                prob_annotated_regions_duration = self.annotated_regions["duration"][
                    annotated_region_indices
                ] / np.sum(self.annotated_regions["duration"][annotated_region_indices])

                # selected one annotated region at random (with probability proportional to its duration)
                annotated_region_index = np.random.choice(
                    annotated_region_indices, p=prob_annotated_regions_duration
                )

                # select one chunk at random in this annotated region
                _, _, start, end = self.annotated_regions[annotated_region_index]
                start_time = rng.uniform(start, end - duration)

                yield self.prepare_chunk(file_id, start_time, duration)

    def train__iter__(self):
        """Iterate over training samples

        Yields
        ------
        dict:
            X: (time, channel)
                Audio chunks.
            y: (frame, )
                Frame-level targets. Note that frame < time.
                `frame` is infered automagically from the
                example model output.
            ...
        """

        # create worker-specific random number generator
        rng = create_rng_for_worker(self.model.current_epoch)

        balance = getattr(self, "balance", None)
        if balance is None:
            chunks = self.train__iter__helper(rng)

        else:
            # create a subchunk generator for each combination of "balance" keys
            subchunks = dict()
            for product in itertools.product(
                [self.metadata_unique_values[key] for key in balance]
            ):
                filters = {key: value for key, value in zip(balance, product)}
                subchunks[product] = self.train__iter__helper(rng, **filters)

        while True:

            # select one subchunk generator at random (with uniform probability)
            # so that it is balanced on average
            if balance is not None:
                chunks = subchunks[rng.choice(subchunks)]

            # generate random chunk
            yield next(chunks)

    def collate_X(self, batch) -> torch.Tensor:
        return default_collate([b["X"] for b in batch])

    def collate_y(self, batch) -> torch.Tensor:

        # gather common set of labels
        # b["y"] is a SlidingWindowFeature instance
        labels = sorted(set(itertools.chain(*(b["y"].labels for b in batch))))

        batch_size, num_frames, num_labels = (
            len(batch),
            len(batch[0]["y"]),
            len(labels),
        )
        Y = np.zeros((batch_size, num_frames, num_labels), dtype=np.int64)

        for i, b in enumerate(batch):
            for local_idx, label in enumerate(b["y"].labels):
                global_idx = labels.index(label)
                Y[i, :, global_idx] = b["y"].data[:, local_idx]

        return torch.from_numpy(Y)

    def collate_fn(self, batch, stage="train"):
        """Collate function used for most segmentation tasks

        This function does the following:
        * stack waveforms into a (batch_size, num_channels, num_samples) tensor batch["X"])
        * apply augmentation when in "train" stage
        * convert targets into a (batch_size, num_frames, num_classes) tensor batch["y"]
        * collate any other keys that might be present in the batch using pytorch default_collate function

        Parameters
        ----------
        batch : list of dict
            List of training samples.

        Returns
        -------
        batch : dict
            Collated batch as {"X": torch.Tensor, "y": torch.Tensor} dict.
        """

        # collate X
        collated_X = self.collate_X(batch)

        # collate y
        collated_y = self.collate_y(batch)

        # apply augmentation (only in "train" stage)
        self.augmentation.train(mode=(stage == "train"))
        augmented = self.augmentation(
            samples=collated_X,
            sample_rate=self.model.hparams.sample_rate,
            targets=collated_y.unsqueeze(1),
        )

        return {
            "X": augmented.samples,
            "y": self.adapt_y(augmented.targets.squeeze(1)),
            "meta": default_collate([b["meta"] for b in batch]),
        }

    def train__len__(self):
        # Number of training samples in one epoch

        duration = np.sum(self.annotated_duration)
        return max(self.batch_size, math.ceil(duration / self.duration))

    def val__getitem__(self, idx):
        validation_chunk = self.validation_chunks[idx]
        return self.prepare_chunk(
            validation_chunk["file_id"],
            validation_chunk["start"],
            duration=validation_chunk["duration"],
        )

    def val__len__(self):
        return len(self.validation_chunks)

    def validation_step(self, batch, batch_idx: int):
        """Compute validation area under the ROC curve

        Parameters
        ----------
        batch : dict of torch.Tensor
            Current batch.
        batch_idx: int
            Batch index.
        """

        X, y = batch["X"], batch["y"]
        # X = (batch_size, num_channels, num_samples)
        # y = (batch_size, num_frames, num_classes) or (batch_size, num_frames)

        y_pred = self.model(X)
        _, num_frames, _ = y_pred.shape
        # y_pred = (batch_size, num_frames, num_classes)

        # - remove warm-up frames
        # - downsample remaining frames
        warm_up_left = round(self.warm_up[0] / self.duration * num_frames)
        warm_up_right = round(self.warm_up[1] / self.duration * num_frames)
        preds = y_pred[:, warm_up_left : num_frames - warm_up_right : 10]
        target = y[:, warm_up_left : num_frames - warm_up_right : 10]

        # torchmetrics tries to be smart about the type of machine learning problem
        # pyannote.audio is more explicit so we have to reshape target and preds for
        # torchmetrics to be happy... more details can be found here:
        # https://torchmetrics.readthedocs.io/en/latest/references/modules.html#input-types

        if self.specifications.problem == Problem.BINARY_CLASSIFICATION:
            # target: shape (batch_size, num_frames), type binary
            # preds:  shape (batch_size, num_frames, 1), type float

            # torchmetrics expects:
            # target: shape (batch_size,), type binary
            # preds:  shape (batch_size,), type float

            self.model.validation_metric(
                preds.reshape(-1),
                target.reshape(-1),
            )

        elif self.specifications.problem == Problem.MULTI_LABEL_CLASSIFICATION:
            # target: shape (batch_size, num_frames, num_classes), type binary
            # preds:  shape (batch_size, num_frames, num_classes), type float

            # torchmetrics expects
            # target: shape (batch_size, num_classes, ...), type binary
            # preds:  shape (batch_size, num_classes, ...), type float

            self.model.validation_metric(
                torch.transpose(preds, 1, 2),
                torch.transpose(target, 1, 2),
            )

        elif self.specifications.problem == Problem.MONO_LABEL_CLASSIFICATION:
            # TODO: implement when pyannote.audio gets its first mono-label segmentation task
            raise NotImplementedError()

        self.model.log_dict(
            self.model.validation_metric,
            on_step=False,
            on_epoch=True,
            prog_bar=True,
            logger=True,
        )

        # log first batch visualization every 2^n epochs.
        if (
            self.model.current_epoch == 0
            or math.log2(self.model.current_epoch) % 1 > 0
            or batch_idx > 0
        ):
            return

        # visualize first 9 validation samples of first batch in Tensorboard/MLflow
        X = X.cpu().numpy()
        y = y.float().cpu().numpy()
        y_pred = y_pred.cpu().numpy()

        # prepare 3 x 3 grid (or smaller if batch size is smaller)
        num_samples = min(self.batch_size, 9)
        nrows = math.ceil(math.sqrt(num_samples))
        ncols = math.ceil(num_samples / nrows)
        fig, axes = plt.subplots(
            nrows=2 * nrows, ncols=ncols, figsize=(8, 5), squeeze=False
        )

        # reshape target so that there is one line per class when plotting it
        y[y == 0] = np.NaN
        if len(y.shape) == 2:
            y = y[:, :, np.newaxis]
        y *= np.arange(y.shape[2])

        # plot each sample
        for sample_idx in range(num_samples):

            # find where in the grid it should be plotted
            row_idx = sample_idx // nrows
            col_idx = sample_idx % ncols

            # plot target
            ax_ref = axes[row_idx * 2 + 0, col_idx]
            sample_y = y[sample_idx]
            ax_ref.plot(sample_y)
            ax_ref.set_xlim(0, len(sample_y))
            ax_ref.set_ylim(-1, sample_y.shape[1])
            ax_ref.get_xaxis().set_visible(False)
            ax_ref.get_yaxis().set_visible(False)

            # plot predictions
            ax_hyp = axes[row_idx * 2 + 1, col_idx]
            sample_y_pred = y_pred[sample_idx]
            ax_hyp.axvspan(0, warm_up_left, color="k", alpha=0.5, lw=0)
            ax_hyp.axvspan(
                num_frames - warm_up_right, num_frames, color="k", alpha=0.5, lw=0
            )
            ax_hyp.plot(sample_y_pred)
            ax_hyp.set_ylim(-0.1, 1.1)
            ax_hyp.set_xlim(0, len(sample_y))
            ax_hyp.get_xaxis().set_visible(False)

        plt.tight_layout()

        for logger in self.model.loggers:
            if isinstance(logger, TensorBoardLogger):
                logger.experiment.add_figure(
                    f"{self.logging_prefix}ValSamples", fig, self.model.current_epoch
                )
            elif isinstance(logger, MLFlowLogger):
                logger.experiment.log_figure(
                    run_id=logger.run_id,
                    figure=fig,
                    artifact_file=f"{self.logging_prefix}ValSamples_epoch{self.model.current_epoch}.png",
                )

        plt.close(fig)
