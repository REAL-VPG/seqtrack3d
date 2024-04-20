# Created by zenn at 2021/4/27
# Modified by Aron Lin at Jun 4 20:32:36 CST 2023 

import numpy as np
import torch
from easydict import EasyDict
from nuscenes.utils import geometry_utils

import datasets.points_utils as points_utils

from datasets.misc_utils import get_history_frame_ids_and_masks, \
    create_history_frame_dict, \
    generate_timestamp_prev_list, \
    generate_virtual_points


def no_processing(data, *args):
    return data


def siamese_processing(data, config, template_transform=None, search_transform=None):
    """

    :param data:
    :param config: {model_bb_scale,model_bb_offset,search_bb_scale, search_bb_offset}
    :return:
    """
    first_frame = data['first_frame']
    template_frame = data['template_frame']
    search_frame = data['search_frame']
    candidate_id = data['candidate_id']
    first_pc, first_box = first_frame['pc'], first_frame['3d_bbox']
    template_pc, template_box = template_frame['pc'], template_frame['3d_bbox']
    search_pc, search_box = search_frame['pc'], search_frame['3d_bbox']
    if template_transform is not None:
        template_pc, template_box = template_transform(template_pc, template_box)
        first_pc, first_box = template_transform(first_pc, first_box)
    if search_transform is not None:
        search_pc, search_box = search_transform(search_pc, search_box)
    # generating template. Merging the object from previous and the first frames.
    if candidate_id == 0:
        samplegt_offsets = np.zeros(3)
    else:
        samplegt_offsets = np.random.uniform(low=-0.3, high=0.3, size=3)
        samplegt_offsets[2] = samplegt_offsets[2] * (5 if config.degrees else np.deg2rad(5))
    template_box = points_utils.getOffsetBB(template_box, samplegt_offsets, limit_box=config.data_limit_box,
                                            degrees=config.degrees)
    model_pc, model_box = points_utils.getModel([first_pc, template_pc], [first_box, template_box],
                                                scale=config.model_bb_scale, offset=config.model_bb_offset)

    assert model_pc.nbr_points() > 20, 'not enough template points'

    # generating search area. Use the current gt box to select the nearby region as the search area.

    if candidate_id == 0 and config.num_candidates > 1:
        sample_offset = np.zeros(3)
    else:
        # gaussian = KalmanFiltering(bnd=[1, 1, (5 if config.degrees else np.deg2rad(5))])
        # sample_offset = gaussian.sample(1)[0]
        raise NotImplementedError("Previously used pomegranate's KalmanFiltering here, now disabled. Update required.")
    sample_bb = points_utils.getOffsetBB(search_box, sample_offset, limit_box=config.data_limit_box,
                                         degrees=config.degrees)
    search_pc_crop = points_utils.generate_subwindow(search_pc, sample_bb,
                                                     scale=config.search_bb_scale, offset=config.search_bb_offset)
    assert search_pc_crop.nbr_points() > 20, 'not enough search points'
    search_box = points_utils.transform_box(search_box, sample_bb)
    seg_label = points_utils.get_in_box_mask(search_pc_crop, search_box).astype(int)
    search_bbox_reg = [search_box.center[0], search_box.center[1], search_box.center[2], -sample_offset[2]]

    template_points, idx_t = points_utils.regularize_pc(model_pc.points.T, config.template_size)
    search_points, idx_s = points_utils.regularize_pc(search_pc_crop.points.T, config.search_size)
    seg_label = seg_label[idx_s]
    data_dict = {
        'template_points': template_points.astype('float32'),
        'search_points': search_points.astype('float32'),
        'box_label': np.array(search_bbox_reg).astype('float32'),
        'bbox_size': search_box.wlh,
        'seg_label': seg_label.astype('float32'),
    }
    if getattr(config, 'box_aware', False):
        template_bc = points_utils.get_point_to_box_distance(template_points, model_box)
        search_bc = points_utils.get_point_to_box_distance(search_points, search_box)
        data_dict.update({'points2cc_dist_t': template_bc.astype('float32'),
                          'points2cc_dist_s': search_bc.astype('float32'), })
    return data_dict


def motion_processing(data, config, template_transform=None, search_transform=None):
    """

    :param data:
    :param config: {model_bb_scale,model_bb_offset,search_bb_scale, search_bb_offset}
    :return:
    point_sample_size
    bb_scale
    bb_offset
    """
    prev_frame = data['prev_frame']
    this_frame = data['this_frame']
    candidate_id = data['candidate_id']
    prev_pc, prev_box = prev_frame['pc'], prev_frame['3d_bbox']
    this_pc, this_box = this_frame['pc'], this_frame['3d_bbox']

    num_points_in_prev_box = geometry_utils.points_in_box(prev_box, prev_pc.points[0:3,:]).sum() 
    assert num_points_in_prev_box > config.limit_num_points_in_prev_box, 'not enough target points'

    if template_transform is not None:
        prev_pc, prev_box = template_transform(prev_pc, prev_box)
    if search_transform is not None:
        this_pc, this_box = search_transform(this_pc, this_box)

    if candidate_id == 0:
        sample_offsets = np.zeros(3) 
    else:
        sample_offsets = np.random.uniform(low=-0.3, high=0.3, size=3)
        sample_offsets[2] = sample_offsets[2] * (5 if config.degrees else np.deg2rad(5))
    ref_box = points_utils.getOffsetBB(prev_box, sample_offsets, limit_box=config.data_limit_box,
                                       degrees=config.degrees)
    prev_frame_pc = points_utils.generate_subwindow(prev_pc, ref_box,
                                                    scale=config.bb_scale,
                                                    offset=config.bb_offset)

    this_frame_pc = points_utils.generate_subwindow(this_pc, ref_box,
                                                    scale=config.bb_scale,
                                                    offset=config.bb_offset)
    # assert this_frame_pc.nbr_points() > config.limit_num_this_frame_subwindow_pc, 'not enough search points'

    this_box = points_utils.transform_box(this_box, ref_box) 
    prev_box = points_utils.transform_box(prev_box, ref_box) 
    ref_box = points_utils.transform_box(ref_box, ref_box)   
    motion_box = points_utils.transform_box(this_box, prev_box) 

    prev_points, idx_prev = points_utils.regularize_pc(prev_frame_pc.points.T, config.point_sample_size) 
    this_points, idx_this = points_utils.regularize_pc(this_frame_pc.points.T, config.point_sample_size) 

    seg_label_this = geometry_utils.points_in_box(this_box, this_points.T[:3,:], 1.25).astype(int) 
    seg_label_prev = geometry_utils.points_in_box(prev_box, prev_points.T[:3,:], 1.25).astype(int) 
    seg_mask_prev = geometry_utils.points_in_box(ref_box, prev_points.T[:3,:], 1.25).astype(float) 
    if candidate_id != 0:
        # Here we use 0.2/0.8 instead of 0/1 to indicate that the previous box is not GT.
        # When boxcloud is used, the actual value of prior-targetness mask doesn't really matter.
        seg_mask_prev[seg_mask_prev == 0] = 0.2
        seg_mask_prev[seg_mask_prev == 1] = 0.8
    seg_mask_this = np.full(seg_mask_prev.shape, fill_value=0.5)

    timestamp_prev = np.full((config.point_sample_size, 1), fill_value=0)
    timestamp_this = np.full((config.point_sample_size, 1), fill_value=0.1)

    prev_points = np.concatenate([prev_points, timestamp_prev, seg_mask_prev[:, None]], axis=-1)
    this_points = np.concatenate([this_points, timestamp_this, seg_mask_this[:, None]], axis=-1)


    stack_points = np.concatenate([prev_points, this_points], axis=0)
    stack_seg_label = np.hstack([seg_label_prev, seg_label_this])
    theta_this = this_box.orientation.degrees * this_box.orientation.axis[-1] if config.degrees else \
        this_box.orientation.radians * this_box.orientation.axis[-1]
    box_label = np.append(this_box.center, theta_this).astype('float32')
    theta_prev = prev_box.orientation.degrees * prev_box.orientation.axis[-1] if config.degrees else \
        prev_box.orientation.radians * prev_box.orientation.axis[-1]
    box_label_prev = np.append(prev_box.center, theta_prev).astype('float32')
    theta_motion = motion_box.orientation.degrees * motion_box.orientation.axis[-1] if config.degrees else \
        motion_box.orientation.radians * motion_box.orientation.axis[-1]
    motion_label = np.append(motion_box.center, theta_motion).astype('float32')

    motion_state_label = np.sqrt(np.sum((this_box.center - prev_box.center) ** 2)) > config.motion_threshold

    data_dict = {
        'points': stack_points.astype('float32'),
        'box_label': box_label,
        'box_label_prev': box_label_prev,
        'motion_label': motion_label,
        'motion_state_label': motion_state_label.astype('int'),
        'bbox_size': this_box.wlh,
        'seg_label': stack_seg_label.astype('int'),
    }

    if getattr(config, 'box_aware', False):
        prev_bc = points_utils.get_point_to_box_distance(stack_points[:config.point_sample_size, :3], prev_box)
        this_bc = points_utils.get_point_to_box_distance(stack_points[config.point_sample_size:, :3], this_box)
        candidate_bc_prev = points_utils.get_point_to_box_distance(stack_points[:config.point_sample_size, :3], ref_box)
        candidate_bc_this = np.zeros_like(candidate_bc_prev)
        candidate_bc = np.concatenate([candidate_bc_prev, candidate_bc_this], axis=0)

        data_dict.update({'prev_bc': prev_bc.astype('float32'),
                          'this_bc': this_bc.astype('float32'),
                          'candidate_bc': candidate_bc.astype('float32')})
    return data_dict

def motion_processing_mf(data, config, template_transform=None, search_transform=None):
    """

    :param data:
    :param config: {model_bb_scale,model_bb_offset,search_bb_scale, search_bb_offset}
    :return:
    point_sample_size
    bb_scale
    bb_offset
    """
    prev_frames = data['prev_frames']
    this_frame = data['this_frame']
    candidate_id = data['candidate_id']
    valid_mask = data['valid_mask']
    num_hist = len(valid_mask)
    empty_counter = 0

    prev_pcs  = [prev_frames[key]['pc'] for key in sorted(prev_frames,key=lambda k: abs(int(k)))] # Ordered point clouds, -1, -2, -3
    prev_boxs = [prev_frames[key]['3d_bbox'] for key in sorted(prev_frames,key=lambda k: abs(int(k)))] # Ordered point clouds, -1, -2, -3
    this_pc, this_box = this_frame['pc'], this_frame['3d_bbox']

    # Check the number of empty boxes
    for prev_box, prev_pc in zip(prev_boxs, prev_pcs):
        num_points_in_prev_box = geometry_utils.points_in_box(prev_box, prev_pc.points[0:3,:]).sum()
        if num_points_in_prev_box < config.limit_num_points_in_prev_box:
            empty_counter += 1
    assert empty_counter < config.empty_box_limit, 'not enough valid box' 

    ref_boxs = []
    for i, prev_box in enumerate(prev_boxs): # Apply a random offset to each box, not uniformly
        if candidate_id == 0:
            sample_offsets = np.zeros(3)
        else:
            sample_offsets = np.random.uniform(low=-0.3, high=0.3, size=3)
            sample_offsets[2] = sample_offsets[2] * (5 if config.degrees else np.deg2rad(5))
        ref_box = points_utils.getOffsetBB(prev_box, sample_offsets, limit_box=config.data_limit_box,
                                        degrees=config.degrees)
        ref_boxs.append(ref_box)


    prev_frame_pcs = []
    for i, prev_pc in enumerate(prev_pcs):
        prev_frame_pc = points_utils.generate_subwindow_with_aroundboxs(prev_pc, ref_boxs[i], ref_boxs[0],
                                                    scale=config.bb_scale,
                                                    offset=config.bb_offset)
        prev_frame_pcs.append(prev_frame_pc)

    this_frame_pc = points_utils.generate_subwindow_with_aroundboxs(this_pc, ref_boxs[0], ref_boxs[0],
                                                    scale=config.bb_scale,
                                                    offset=config.bb_offset)

    this_box    = points_utils.transform_box(this_box, ref_boxs[0]) 
    prev_boxs   = [points_utils.transform_box(prev_box, ref_boxs[0]) for prev_box in prev_boxs] 
    ref_boxs    = [points_utils.transform_box(ref_box, ref_boxs[0]) for ref_box in ref_boxs]    
    motion_boxs = [points_utils.transform_box(this_box, prev_box) for prev_box in prev_boxs]  

    # Resample each frame of the point cloud to a specific number
    prev_points_list = [points_utils.regularize_pc(prev_frame_pc.points.T, config.point_sample_size)[0] for prev_frame_pc in prev_frame_pcs] 
    this_points = points_utils.regularize_pc(this_frame_pc.points.T, config.point_sample_size)[0] 

    seg_label_this = geometry_utils.points_in_box(this_box, this_points.T[:3,:], config.bb_scale).astype(int)
    seg_label_prev_list = [geometry_utils.points_in_box(prev_box, prev_points.T[:3,:], config.bb_scale).astype(int) for prev_box, prev_points in zip(prev_boxs, prev_points_list)] #应当只考虑xyz特征
    seg_mask_prev_list = [geometry_utils.points_in_box(ref_box, prev_points.T[:3,:], config.bb_scale).astype(float) for ref_box,prev_points in zip(ref_boxs,prev_points_list)]#应当只考虑xyz特征
    if candidate_id != 0:
        for seg_mask_prev in seg_mask_prev_list:
            # Here we use 0.2/0.8 instead of 0/1 to indicate that the previous box is not GT.
            # When boxcloud is used, the actual value of prior-targetness mask doesn't really matter.
            seg_mask_prev[seg_mask_prev == 0] = 0.2
            seg_mask_prev[seg_mask_prev == 1] = 0.8
    seg_mask_this = np.full(seg_mask_prev_list[0].shape, fill_value=0.5)


    timestamp_prev_list = generate_timestamp_prev_list(valid_mask,config.point_sample_size)
    timestamp_this = np.full((config.point_sample_size, 1), fill_value=0.1)

    prev_points_list = [
        np.concatenate([prev_points, timestamp_prev, seg_mask_prev[:, None]],
                       axis=-1)
        for prev_points, timestamp_prev, seg_mask_prev in zip(
            prev_points_list, timestamp_prev_list, seg_mask_prev_list)
    ]
    this_points = np.concatenate(
        [this_points, timestamp_this, seg_mask_this[:, None]], axis=-1)

    stack_points_list = prev_points_list + [this_points]
    stack_points = np.concatenate(stack_points_list, axis=0)

    stack_seg_label_list = seg_label_prev_list + [seg_label_this]
    stack_seg_label = np.hstack(stack_seg_label_list)

    theta_this = this_box.orientation.degrees * this_box.orientation.axis[-1] if config.degrees else \
        this_box.orientation.radians * this_box.orientation.axis[-1]
    box_label = np.append(this_box.center, theta_this).astype('float32')
    theta_prev_list = [
        prev_box.orientation.degrees * prev_box.orientation.axis[-1]
        if config.degrees else prev_box.orientation.radians *
        prev_box.orientation.axis[-1] for prev_box in prev_boxs
    ]
    box_label_prev_list = [
        np.append(prev_box.center, theta_prev).astype('float32')
        for prev_box, theta_prev in zip(prev_boxs, theta_prev_list)
    ]

    # Generate a reference box sequence
    theta_ref_list=[
        ref_box.orientation.degrees * ref_box.orientation.axis[-1]
        if config.degrees else ref_box.orientation.radians *
        ref_box.orientation.axis[-1] for ref_box in ref_boxs
    ]
    ref_box_list = [
        np.append(ref_box.center, theta_ref).astype('float32')
        for ref_box, theta_ref in zip(ref_boxs, theta_ref_list)
    ]

    theta_motion_list = [
        motion_box.orientation.degrees * motion_box.orientation.axis[-1]
        if config.degrees else motion_box.orientation.radians *
        motion_box.orientation.axis[-1] for motion_box in motion_boxs
    ]

    motion_label_list = [
        np.append(motion_box.center, theta_motion).astype('float32')
        for motion_box, theta_motion in zip(motion_boxs, theta_motion_list)
    ]
    motion_state_label_list = [ 
        np.sqrt(np.sum((this_box.center - prev_box.center)**2))
        > config.motion_threshold for prev_box in prev_boxs
    ]

    data_dict = {
        'points': stack_points.astype('float32'), # Historical first, then current
        'box_label': box_label, 
        'ref_boxs':np.stack(ref_box_list, axis=0),
        'box_label_prev': np.stack(box_label_prev_list, axis=0),
        'motion_label': np.stack(motion_label_list, axis=0),
        'motion_state_label': np.stack(motion_state_label_list, axis=0).astype('int'),
        'bbox_size': this_box.wlh, 
        'seg_label': stack_seg_label.astype('int'), 
        'valid_mask': np.array(valid_mask).astype('int'), 
    }

    if getattr(config, 'box_aware', False):
        stack_points_split = np.split(stack_points, num_hist + 1, axis=0)
        hist_points_list = stack_points_split[:num_hist] 
        prev_bc_list = [
            points_utils.get_point_to_box_distance(hist_points[:, :3], prev_box)
            for hist_points, prev_box in zip(hist_points_list, prev_boxs)
        ]
        this_points_split = stack_points_split[-1] 
        this_bc = points_utils.get_point_to_box_distance(this_points_split[:,:3], this_box)


        candidate_bc_prev_list = [
            points_utils.get_point_to_box_distance(hist_points[:, :3], prev_box)
            for hist_points, prev_box in zip(hist_points_list, ref_boxs)
        ]

        candidate_bc_this = np.zeros_like(candidate_bc_prev_list[0])
        candidate_bc_prev_list = candidate_bc_prev_list + [candidate_bc_this]
        candidate_bc = np.concatenate(candidate_bc_prev_list, axis=0)

        data_dict.update({'prev_bc': np.stack(prev_bc_list, axis=0).astype('float32'),
                          'this_bc': this_bc.astype('float32'),
                          'candidate_bc': candidate_bc.astype('float32')})

    return data_dict


class PointTrackingSampler(torch.utils.data.Dataset):
    def __init__(self, dataset, random_sample, sample_per_epoch=10000, processing=siamese_processing, config=None,
                 **kwargs):
        if config is None:
            config = EasyDict(kwargs)
        self.sample_per_epoch = sample_per_epoch
        self.dataset = dataset
        self.processing = processing
        self.config = config
        self.random_sample = random_sample
        self.num_candidates = getattr(config, 'num_candidates', 1)
        if getattr(self.config, "use_augmentation", False):
            print('using augmentation')
            self.transform = points_utils.apply_augmentation
        else:
            self.transform = None
        if not self.random_sample:
            num_frames_total = 0
            self.tracklet_start_ids = [num_frames_total]
            for i in range(dataset.get_num_tracklets()):
                num_frames_total += dataset.get_num_frames_tracklet(i)
                self.tracklet_start_ids.append(num_frames_total)

    def get_anno_index(self, index):
        return index // self.num_candidates 

    def get_candidate_index(self, index):
        return index % self.num_candidates

    def __len__(self):
        if self.random_sample:
            return self.sample_per_epoch * self.num_candidates
        else:
            return self.dataset.get_num_frames_total() * self.num_candidates

    def __getitem__(self, index):
        anno_id = self.get_anno_index(index)
        candidate_id = self.get_candidate_index(index)
        try:
            if self.random_sample:
                tracklet_id = torch.randint(0, self.dataset.get_num_tracklets(), size=(1,)).item()
                tracklet_annos = self.dataset.tracklet_anno_list[tracklet_id]
                frame_ids = [0] + points_utils.random_choice(num_samples=2, size=len(tracklet_annos)).tolist()
            else:
                for i in range(0, self.dataset.get_num_tracklets()):
                    if self.tracklet_start_ids[i] <= anno_id < self.tracklet_start_ids[i + 1]:
                        tracklet_id = i 
                        this_frame_id = anno_id - self.tracklet_start_ids[i] 
                        prev_frame_id = max(this_frame_id - 1, 0) 
                        frame_ids = (0, prev_frame_id, this_frame_id) 
            first_frame, template_frame, search_frame = self.dataset.get_frames(tracklet_id, frame_ids=frame_ids)
            data = {"first_frame": first_frame,
                    "template_frame": template_frame,
                    "search_frame": search_frame,
                    "candidate_id": candidate_id}

            return self.processing(data, self.config,
                                   template_transform=None,
                                   search_transform=self.transform)
        except AssertionError:
            return self[torch.randint(0, len(self), size=(1,)).item()]


class TestTrackingSampler(torch.utils.data.Dataset):
    def __init__(self, dataset, config=None, **kwargs):
        if config is None:
            config = EasyDict(kwargs)
        self.dataset = dataset
        self.config = config

    def __len__(self):
        return self.dataset.get_num_tracklets()

    def __getitem__(self, index):
        tracklet_annos = self.dataset.tracklet_anno_list[index]
        frame_ids = list(range(len(tracklet_annos)))
        return self.dataset.get_frames(index, frame_ids)


class MotionTrackingSampler(PointTrackingSampler):
    def __init__(self, dataset, config=None, **kwargs):
        super().__init__(dataset, random_sample=False, config=config, **kwargs)
        self.processing = motion_processing

    def __getitem__(self, index):
        anno_id = self.get_anno_index(index)
        candidate_id = self.get_candidate_index(index) 
        try:

            for i in range(0, self.dataset.get_num_tracklets()):
                if self.tracklet_start_ids[i] <= anno_id < self.tracklet_start_ids[i + 1]:
                    tracklet_id = i
                    this_frame_id = anno_id - self.tracklet_start_ids[i]
                    prev_frame_id = max(this_frame_id - 1, 0)
                    frame_ids = (0, prev_frame_id, this_frame_id)
            first_frame, prev_frame, this_frame = self.dataset.get_frames(tracklet_id, frame_ids=frame_ids)
            data = {
                "first_frame": first_frame, 
                "prev_frame": prev_frame,  
                "this_frame": this_frame,   
                "candidate_id": candidate_id}
        
            return self.processing(data, self.config,
                                   template_transform=self.transform,
                                   search_transform=self.transform)
        except AssertionError:
            return self[torch.randint(0, len(self), size=(1,)).item()]


class MotionTrackingSamplerMF(PointTrackingSampler):
    def __init__(self, dataset, config=None, **kwargs):
        super().__init__(dataset, random_sample=False, config=config, **kwargs)
        self.processing = motion_processing_mf

    def __getitem__(self, index):
        anno_id = self.get_anno_index(index)
        candidate_id = self.get_candidate_index(index) 
        try:
            for i in range(0, self.dataset.get_num_tracklets()):
                if self.tracklet_start_ids[i] <= anno_id < self.tracklet_start_ids[i + 1]:
                    tracklet_id = i
                    this_frame_id = anno_id - self.tracklet_start_ids[i]
                    prev_frame_ids, valid_mask = get_history_frame_ids_and_masks(this_frame_id,self.dataset.hist_num)

                    frame_ids = (0, this_frame_id)

            first_frame, this_frame = self.dataset.get_frames(tracklet_id, frame_ids=frame_ids)
            prev_frames_tuple = self.dataset.get_frames(tracklet_id, frame_ids=prev_frame_ids)
            prev_frames_dict = create_history_frame_dict(prev_frames_tuple)
            data = {
                "first_frame": first_frame, 
                "prev_frames": prev_frames_dict,  
                "this_frame": this_frame,   
                "candidate_id": candidate_id,
                "valid_mask":valid_mask,}
            
            return self.processing(data, self.config,
                                   template_transform=self.transform,
                                   search_transform=self.transform)
        except AssertionError:
            # return 1
            return self[torch.randint(0, len(self), size=(1,)).item()]
