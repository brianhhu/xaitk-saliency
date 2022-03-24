import numpy as np
from typing import Optional, Union, Sequence, List, Dict, Any, Type, TypeVar, Iterable, Tuple, Hashable

from xaitk_saliency.interfaces.gen_object_detector_blackbox_sal import GenerateObjectDetectorBlackboxSaliency
from xaitk_saliency.interfaces.perturb_image import PerturbImage
from xaitk_saliency.interfaces.gen_detector_prop_sal import GenerateDetectorProposalSaliency
from xaitk_saliency.utils.detection import format_detection
from xaitk_saliency.utils.masking import occlude_image_batch

from smqtk_detection.utils.bbox import AxisAlignedBoundingBox
from smqtk_detection.interfaces.detect_image_objects import DetectImageObjects
from smqtk_core.configuration import (
    from_config_dict,
    make_default_config,
    to_config_dict,
)

C = TypeVar("C", bound="PerturbationOcclusion")


class PerturbationOcclusion (GenerateObjectDetectorBlackboxSaliency):
    """
    Generator composed of modular perturbation and occlusion-based algorithms.

    This implementation exposes a public attribute `fill`.
    This may be set to a scalar or sequence value to indicate a color that
    should be used for filling occluded areas as determined by the given
    `PerturbImage` implementation.
    This is a parameter to be set during runtime as this is most often driven
    by the black-box algorithm used, if at all.

    :param perturber: `PerturbImage` implementation instance for generating
        occlusion masks.
    :param generator: `GenerateDetectorProposalSaliency` implementation
        instance for generating saliency masks given occlusion masks and
        black-box detector outputs.
    :param threads: Optional number threads to use to enable parallelism in
        applying perturbation masks to an input image.
        If 0, a negative value, or `None`, work will be performed on the
        main-thread in-line.
    """

    def __init__(
        self,
        perturber: PerturbImage,
        generator: GenerateDetectorProposalSaliency,
        threads: Optional[int] = 0
    ):
        self._perturber = perturber
        self._generator = generator
        self._threads = threads
        # Optional fill color
        self.fill: Optional[Union[int, Sequence[int]]] = None

    def _generate(
        self,
        ref_image: np.ndarray,
        bboxes: np.ndarray,
        scores: np.ndarray,
        blackbox: DetectImageObjects,
        objectness: Optional[np.ndarray] = None,
    ) -> np.ndarray:
        ref_dets_mat = format_detection(bboxes, scores, objectness)

        pert_masks = self._perturber(ref_image)

        pert_dets = blackbox.detect_objects(
            occlude_image_batch(
                ref_image,
                pert_masks,
                fill=self.fill,
                threads=self._threads
            )
        )

        pert_dets_mat = _dets_to_formatted_mat(pert_dets)

        return self._generator(
            ref_dets_mat,
            pert_dets_mat,
            pert_masks
        )

    @classmethod
    def get_default_config(cls) -> Dict[str, Any]:
        cfg = super().get_default_config()
        cfg['perturber'] = make_default_config(PerturbImage.get_impls())
        cfg['generator'] = make_default_config(GenerateDetectorProposalSaliency.get_impls())
        return cfg

    @classmethod
    def from_config(
        cls: Type[C],
        config_dict: Dict,
        merge_default: bool = True
    ) -> C:
        config_dict = dict(config_dict)  # shallow-copy
        config_dict['perturber'] = from_config_dict(
            config_dict['perturber'],
            PerturbImage.get_impls()
        )
        config_dict['generator'] = from_config_dict(
            config_dict['generator'],
            GenerateDetectorProposalSaliency.get_impls()
        )
        return super().from_config(config_dict, merge_default=merge_default)

    def get_config(self) -> Dict[str, Any]:
        return {
            "perturber": to_config_dict(self._perturber),
            "generator": to_config_dict(self._generator),
            "threads": self._threads,
        }


def _dets_to_formatted_mat(
    dets: Iterable[Iterable[Tuple[AxisAlignedBoundingBox, Dict[Hashable, float]]]],
) -> np.ndarray:
    """
    Converts detections, as returned by an implementation of
    ``DetectImageObjects``, into a detection matrix formatted for use with
    an implementation of ``GenerateDetectorProposalSaliency``.
    The order of the class scores in the resulting matrix follows the order of
    labels present in the first non-empty detection in the input set.

    :param dets: Detections, as returned by an implementation of
        ``DetectImageObjects``.

    :returns: Matrix of detections with shape
        [nImgs x nDets x (4+1+nClasses)].
        If the number of detections for each image is not consistent, the matrix
        will be padded with rows of ones, except for the objectness which is set
        to zero.
    """
    labels = []  # type: Sequence[Hashable]
    num_classes = 0
    dets_mat_list = []  # type: List[np.ndarray]
    for img_idx, img_dets in enumerate(dets):

        img_bboxes = np.array([])
        img_scores = np.array([])
        img_objectness = np.array([])

        # reshape for vertical stacking
        img_bboxes.shape = (0, 4)
        img_scores.shape = (0, num_classes)

        for det in img_dets:
            obj = 1.0
            bbox = det[0]
            score_dict = det[1]

            # use class labels of first non-empty detection
            if num_classes == 0:
                labels = list(score_dict.keys())
                num_classes = len(labels)
                img_scores.shape = (0, num_classes)
                # reshape previous mats for padding later
                dets_mat_list[0:img_idx] = [
                    np.array([]).reshape(0, 4+1+num_classes) for _ in range(img_idx)
                ]

            scores = []
            for label in labels:
                scores.append(score_dict[label])

            # single class score only
            if len([score for score in scores if score > 0]) == 1:
                conf = max(scores)
                obj = conf  # replace objectness with class score
                scores[scores.index(conf)] = 1  # one-hot encode class score

            img_bboxes = np.vstack((img_bboxes, [*bbox.min_vertex, *bbox.max_vertex]))
            img_scores = np.vstack((img_scores, scores))
            img_objectness = np.hstack((img_objectness, obj))

        dets_mat_list.append(format_detection(img_bboxes, img_scores, img_objectness))

    # pad matrices
    num_dets = [dets_mat.shape[0] for dets_mat in dets_mat_list]
    max_dets = max(num_dets)
    pad_row = np.ones(4+1+num_classes)
    # set objectness to zero
    pad_row[4] = 0
    for i, dets_mat in enumerate(dets_mat_list):
        size_diff = max_dets - dets_mat.shape[0]
        dets_mat_list[i] = np.vstack((dets_mat, np.tile(pad_row, (size_diff, 1))))

    return np.asarray(dets_mat_list)