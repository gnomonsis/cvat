
# Copyright (C) 2020 Intel Corporation
#
# SPDX-License-Identifier: MIT

from functools import reduce
from itertools import chain, zip_longest

import numpy as np

from datumaro.components.extractor import AnnotationType, Bbox, LabelCategories
from datumaro.components.project import Dataset
from datumaro.util import find
from datumaro.util.annotation_tools import compute_bbox, iou as segment_iou


SEGMENT_TYPES = {
    AnnotationType.bbox,
    AnnotationType.polygon,
    AnnotationType.mask
}

def get_segments(anns, conf_threshold=1.0):
    return [ann for ann in anns \
        if conf_threshold <= ann.attributes.get('score', 1) and \
            ann.type in SEGMENT_TYPES
    ]

def merge_annotations_unique(a, b):
    merged = []
    for item in chain(a, b):
        found = False
        for elem in merged:
            if elem == item:
                found = True
                break
        if not found:
            merged.append(item)

    return merged

def merge_categories(sources):
    categories = {}
    for source in sources:
        categories.update(source)
    for source in sources:
        for cat_type, source_cat in source.items():
            if not categories[cat_type] == source_cat:
                raise NotImplementedError(
                    "Merging different categories is not implemented yet")
    return categories

def merge_datasets(sources, iou_threshold=1.0, conf_threshold=1.0):
    # TODO: put this function to the right place
    merged = Dataset(
        categories=merge_categories([s.categories() for s in sources]))
    for item_a in sources[0]:
        items = [s.get(item_a.id, subset=item_a.subset) for s in sources[1:]]

        source_annotations = [a for item in items for a in item.annotations
            if conf_threshold <= a.attributes.get('score', 1)]
        annotations = merge_annotations_multi_match(source_annotations,
            iou_threshold=iou_threshold)
        merged.put(item_a.wrap(image=Dataset._merge_images(item_a, item_b),
            annotations=annotations))
    return merged

def merge_annotations_multi_match(sources, iou_threshold=None):
    segments = [a for s in sources for a in s if a.type in SEGMENT_TYPES]
    annotations = merge_segments(segments, iou_threshold=iou_threshold)

    non_segments = [a for s in sources for a in s if a.type not in SEGMENT_TYPES]
    annotations += reduce(merge_annotations_unique, non_segments, [])

    return annotations

def merge_labels(sources):
    votes = {} # label -> score
    for s in chain(*sources):
        for label_ann in s:
            votes[label_ann.label] = 1.0 + votes.get(value, 0.0)

    labels = {}
    for name, votes in votes.items():
        labels[name] = max(votes.items(), key=lambda e: e[1])[0]

    return labels

def merge_segments(sources, iou_threshold=1.0, ignored_attributes=None):
    ignored_attributes = ignored_attributes or set()

    clusters = find_segment_clusters(sources, pairwise_iou=iou_threshold)
    group_map = find_cluster_groups(clusters)

    merged = []
    for cluster_id, cluster in enumerate(clusters):
        label, label_score = find_cluster_label(cluster)
        bbox = compute_bbox(cluster)
        segm_score = sum(max(0, segment_iou(Bbox(*bbox), s))
            for s in cluster) / len(cluster)

        attributes = find_cluster_attrs(cluster)
        attributes = { k: v for k, v in attributes.items()
            if k not in ignored_attributes }

        score = label_score * segm_score
        attributes['score'] = score if label is not None else None

        group_id, cluster_group, ann_groups = find(enumerate(group_map),
            lambda e: cluster_id in e[1][0])
        if not ann_groups or len(cluster_group) == 1:
            group_id = None

        merged.append(Bbox(*bbox, label=label, group=group_id,
            attributes=attributes))
    return merged

def find_cluster_label(cluster):
    label_votes = {}
    votes_count = 0
    for s in cluster:
        if s.label is None:
            continue

        weight = s.attributes.get('score', 1.0)
        label_votes[s.label] = weight + label_votes.get(s.label, 0.0)
        votes_count += 1

    label, score = max(label_votes.items(), key=lambda e: e[1], default=None)
    score = score / votes_count if votes_count else None
    return label, score

def find_cluster_groups(clusters):
    cluster_groups = []
    visited = set()
    for a_idx, cluster_a in enumerate(clusters):
        if a_idx in visited:
            continue
        visited.add(a_idx)

        cluster_group = { a_idx }

        # find segment groups in the cluster group
        a_groups = set(ann.group for ann in cluster_a)
        for cluster_b in clusters[a_idx+1 :]:
            b_groups = set(ann.group for ann in cluster_b)
            if a_groups & b_groups:
                a_groups |= b_groups

        # now we know all the segment groups in this cluster group
        # so we can find adjacent clusters
        for b_idx, cluster_b in enumerate(clusters[a_idx+1 :]):
            b_idx = a_idx + 1 + b_idx
            b_groups = set(ann.group for ann in cluster_b)
            if a_groups & b_groups:
                cluster_group.add(b_idx)
                visited.add(b_idx)

        cluster_groups.append( (cluster_group, a_groups) )
    return cluster_groups

def find_cluster_attrs(cluster):
    # TODO: when attribute types are implemented, add linear
    # interpolation for contiguous values

    attr_votes = {} # name -> { value: score , ... }
    for s in cluster:
        for name, value in s.attributes.items():
            votes = attr_votes.get(name, {})
            votes[value] = 1.0 + votes.get(value, 0.0)
            attr_votes[name] = votes

    attributes = {}
    for name, votes in attr_votes.items():
        attributes[name] = max(votes.items(), key=lambda e: e[1])[0]

    return attributes

def find_segment_clusters(sources, pairwise_iou=None, cluster_iou=None):
    if pairwise_iou is None: pairwise_iou = 0.9
    if cluster_iou is None: cluster_iou = pairwise_iou

    sources = [get_segments(source) for source in sources]
    id_segm = { id(sgm): (sgm, src_i)
        for src_i, src in enumerate(sources) for sgm in src }

    def _is_close_enough(cluster, extra_id):
        # check if whole cluster IoU will not be broken
        # when this segment is added
        b = id_segm[extra_id][0]
        for a_id in cluster:
            a = id_segm[a_id][0]
            if segment_iou(a, b) < cluster_iou:
                return False
        return True

    def _has_same_source(cluster, extra_id):
        b = id_segm[extra_id][1]
        for a_id in cluster:
            a = id_segm[a_id][1]
            if a == b:
                return True
        return False

    # match segments in sources, pairwise
    adjacent = { i: [] for i in id_segm } # id(sgm) -> [id(adj_sgm1), ...]
    for a_idx, src_a in enumerate(sources):
        for src_b in sources[a_idx+1 :]:
            matches, mismatches, _, _ = \
                compare_segments(src_a, src_b, pairwise_iou)
            for m in matches + mismatches:
                adjacent[id(m[0])].append(id(m[1]))

    # join all segments into matching clusters
    clusters = []
    visited = set()
    for cluster_idx in adjacent:
        if cluster_idx in visited:
            continue

        cluster = set()
        to_visit = { cluster_idx }
        while to_visit:
            c = to_visit.pop()
            cluster.add(c)
            visited.add(c)

            for i in adjacent[c]:
                if i in visited:
                    continue
                if 0 < cluster_iou and not _is_close_enough(cluster, i):
                    continue
                if _has_same_source(cluster, i):
                    continue

                to_visit.add(i)

        clusters.append([id_segm[i][0] for i in cluster])

    return clusters

def compare_segments(a_segms, b_segms, iou_threshold=1.0):
    a_segms.sort(key=lambda ann: 1 - ann.attributes.get('score', 1))
    b_segms.sort(key=lambda ann: 1 - ann.attributes.get('score', 1))

    # a_matches: indices of b_segms matched to a bboxes
    # b_matches: indices of a_segms matched to b bboxes
    a_matches = -np.ones(len(a_segms), dtype=int)
    b_matches = -np.ones(len(b_segms), dtype=int)

    ious = np.array([[segment_iou(a, b) for b in b_segms] for a in a_segms])

    # matches: boxes we succeeded to match completely
    # mispred: boxes we succeeded to match, having label mismatch
    matches = []
    mispred = []

    for a_idx, a_segm in enumerate(a_segms):
        if len(b_segms) == 0:
            break
        matched_b = a_matches[a_idx]
        iou_max = max(ious[a_idx, matched_b], iou_threshold)
        for b_idx, b_segm in enumerate(b_segms):
            if 0 <= b_matches[b_idx]: # assign a_segm with max conf
                continue
            iou = ious[a_idx, b_idx]
            if iou < iou_max:
                continue
            iou_max = iou
            matched_b = b_idx

        if matched_b < 0:
            continue
        a_matches[a_idx] = matched_b
        b_matches[matched_b] = a_idx

        b_segm = b_segms[matched_b]

        if a_segm.label == b_segm.label:
            matches.append( (a_segm, b_segm) )
        else:
            mispred.append( (a_segm, b_segm) )

    # *_umatched: boxes of (*) we failed to match
    a_unmatched = [a_segms[i] for i, m in enumerate(a_matches) if m < 0]
    b_unmatched = [b_segms[i] for i, m in enumerate(b_matches) if m < 0]

    return matches, mispred, a_unmatched, b_unmatched

class Comparator:
    def __init__(self, iou_threshold=0.5, conf_threshold=0.9):
        self.iou_threshold = iou_threshold
        self.conf_threshold = conf_threshold

    # pylint: disable=no-self-use
    def compare_dataset_labels(self, extractor_a, extractor_b):
        a_label_cat = extractor_a.categories().get(AnnotationType.label)
        b_label_cat = extractor_b.categories().get(AnnotationType.label)
        if not a_label_cat and not b_label_cat:
            return None
        if not a_label_cat:
            a_label_cat = LabelCategories()
        if not b_label_cat:
            b_label_cat = LabelCategories()

        mismatches = []
        for a_label, b_label in zip_longest(a_label_cat.items, b_label_cat.items):
            if a_label != b_label:
                mismatches.append((a_label, b_label))
        return mismatches
    # pylint: enable=no-self-use

    def compare_item_labels(self, item_a, item_b):
        conf_threshold = self.conf_threshold

        a_labels = set(ann.label for ann in item_a.annotations \
            if ann.type is AnnotationType.label and \
               conf_threshold < ann.attributes.get('score', 1))
        b_labels = set(ann.label for ann in item_b.annotations \
            if ann.type is AnnotationType.label and \
               conf_threshold < ann.attributes.get('score', 1))

        a_unmatched = a_labels - b_labels
        b_unmatched = b_labels - a_labels
        matches = a_labels & b_labels

        return matches, a_unmatched, b_unmatched

    def compare_item_bboxes(self, item_a, item_b):
        a_boxes = get_segments(item_a.annotations, self.conf_threshold)
        b_boxes = get_segments(item_b.annotations, self.conf_threshold)
        return compare_segments(a_boxes, b_boxes,
            iou_threshold=self.iou_threshold)