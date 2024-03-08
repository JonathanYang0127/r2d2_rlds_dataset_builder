from typing import Iterator, Tuple, Any, Dict, Union, Callable, Iterable

import glob
import numpy as np
import tensorflow as tf
import tensorflow_datasets as tfds
import tensorflow_hub as hub
import os
import cv2
import h5py
import json
import tqdm
import io
from collections import defaultdict
import random
from copy import deepcopy
from PIL import Image

import itertools
from multiprocessing import Pool
from functools import partial
from tensorflow_datasets.core import download
from tensorflow_datasets.core import split_builder as split_builder_lib
from tensorflow_datasets.core import naming
from tensorflow_datasets.core import splits as splits_lib
from tensorflow_datasets.core import utils
# from tensorflow_datasets.core import writer as writer_lib
from tensorflow_datasets.core import example_serializer
from tensorflow_datasets.core import dataset_builder
from tensorflow_datasets.core import file_adapters
from r2_d2.resume_writer import ResumeWriter

Key = Union[str, int]
# The nested example dict passed to `features.encode_example`
Example = Dict[str, Any]
KeyExample = Tuple[Key, Example]

N_WORKERS = 40 # number of parallel workers for data conversion
MAX_PATHS_IN_MEMORY = 600            # number of paths converted & stored in memory before writing to disk
                                    # -> the higher the faster / more parallel conversion, adjust based on avilable RAM
                                    # note that one path may yield multiple episodes and adjust accordingly

# optionally provide info to resume conversion from a previous run
#RESUME_DIR = "/nfs/kun2/datasets/r2d2/tfds/r2_d2/1.0.0.incomplete0DSARA"
RESUME_DIR = None #"/nfs/kun2/datasets/r2d2/tfds/r2_d2/r2_d2/1.0.0.incomplete22QEZD"
START_CHUNK = 0 #124


#_embed = hub.load("https://tfhub.dev/google/universal-sentence-encoder-large/5")
#language_instruction = ''
#dummy_language_embedding = _embed([language_instruction])[0].numpy()

with open("/nfs/kun2/datasets/r2d2/aggregated-annotations-030724.json", "r") as F:
    language_annotations = json.load(F)

# remove pilot vs batch etc leading word in key
#cleaned_language_annotations = {}
#for key in tqdm.tqdm(language_annotations.keys()):
#    cleaned_language_annotations[key.split('/')[-1]] = language_annotations[key]


#def get_language_annotations(key):
#    annot = cleaned_language_annotations[key]
#    return (
#        annot.get("language_instruction1", ''), # + annot.get("language_instruction1_label", ''),
#        annot.get("language_instruction2", ''), # + annot.get("language_instruction2_label_1", ''),
#        annot.get("language_instruction3", ''), # + annot.get("language_instruction2_label_2", '')
#    )


# pre-compute all Kona embeddings
#language_annotation_embeddings = dict()
#for key in tqdm.tqdm(language_annotations.keys()):
#    annot = cleaned_language_annotations[key]
    #embed_1, embed_2, embed_3 = tuple(_embed(get_language_annotations(key)).numpy())
    #language_annotation_embeddings[key] = dict(
    #    language_embedding=embed_1,
    #    language_embedding_2=embed_2,
    #    language_embedding_3=embed_3,
    #)


camera_type_dict = {
    'hand_camera_id': 0,
    'varied_camera_1_id': 1,
    'varied_camera_2_id': 1,
}

camera_type_to_string_dict = {
    0: "hand_camera",
    1: "varied_camera",
    2: "fixed_camera",
}


def get_camera_type(cam_id):
    if cam_id not in camera_type_dict:
        return None
    type_int = camera_type_dict[cam_id]
    type_str = camera_type_to_string_dict[type_int]
    return type_str


class MP4Reader:
    def __init__(self, filepath, serial_number):
        # Save Parameters #
        self.serial_number = serial_number
        self._index = 0

        # Open Video Reader #
        self._mp4_reader = cv2.VideoCapture(filepath)
        if not self._mp4_reader.isOpened():
            raise RuntimeError("Corrupted MP4 File")

    def set_reading_parameters(
        self,
        image=True,
        concatenate_images=False,
        resolution=(0, 0),
        resize_func=None,
    ):
        # Save Parameters #
        self.image = image
        self.concatenate_images = concatenate_images
        self.resolution = resolution
        self.resize_func = cv2.resize
        self.skip_reading = not image
        if self.skip_reading:
            return

    def get_frame_resolution(self):
        width = self._mp4_reader.get(cv2.cv.CV_CAP_PROP_FRAME_WIDTH)
        height = self._mp4_reader.get(cv2.cv.CV_CAP_PROP_FRAME_HEIGHT)
        return (width, height)

    def get_frame_count(self):
        if self.skip_reading:
            return 0
        frame_count = int(self._mp4_reader.get(cv2.cv.CV_CAP_PROP_FRAME_COUNT))
        return frame_count

    def set_frame_index(self, index):
        if self.skip_reading:
            return

        if index < self._index:
            self._mp4_reader.set(cv2.CAP_PROP_POS_FRAMES, index - 1)
            self._index = index

        while self._index < index:
            self.read_camera(ignore_data=True)

    def _process_frame(self, frame):
        frame = deepcopy(frame)
        if self.resolution == (0, 0):
            return frame
        return self.resize_func(frame, self.resolution)
        # return cv2.resize(frame, self.resolution)#, interpolation=cv2.INTER_AREA)

    def read_camera(self, ignore_data=False, correct_timestamp=None):
        # Skip if Read Unnecesary #
        if self.skip_reading:
            return {}

        # Read Camera #
        success, frame = self._mp4_reader.read()

        self._index += 1
        if not success:
            return None
        if ignore_data:
            return None

        # Return Data #
        data_dict = {}

        if self.concatenate_images or 'stereo' not in self.serial_number:
            data_dict["image"] = {self.serial_number: self._process_frame(frame)}
        else:
            single_width = frame.shape[1] // 2
            data_dict["image"] = {
                self.serial_number + "_left": self._process_frame(frame[:, :single_width, :]),
                self.serial_number + "_right": self._process_frame(frame[:, single_width:, :]),
            }

        return data_dict

    def disable_camera(self):
        if hasattr(self, "_mp4_reader"):
            self._mp4_reader.release()


class RecordedMultiCameraWrapper:
    def __init__(self, recording_folderpath, camera_kwargs={}):
        # Save Camera Info #
        self.camera_kwargs = camera_kwargs

        # Open Camera Readers #
        svo_filepaths = [] #glob.glob(recording_folderpath + "/*.svo")
        mp4_filepaths = glob.glob(recording_folderpath + "/*.mp4")
        all_filepaths = svo_filepaths + mp4_filepaths

        self.camera_dict = {}
        for f in all_filepaths:
            serial_number = f.split("/")[-1][:-4]
            cam_type = get_camera_type(serial_number)
            camera_kwargs.get(cam_type, {})

            if f.endswith(".svo"):
                Reader = SVOReader
            elif f.endswith(".mp4"):
                Reader = MP4Reader
            else:
                raise ValueError

            self.camera_dict[serial_number] = Reader(f, serial_number)

    def read_cameras(self, index=None, camera_type_dict={}, timestamp_dict={}):
        full_obs_dict = defaultdict(dict)

        # Read Cameras In Randomized Order #
        all_cam_ids = list(self.camera_dict.keys())
        #random.shuffle(all_cam_ids)

        for cam_id in all_cam_ids:
            if 'stereo' in cam_id: continue
            try:
                cam_type = camera_type_dict[cam_id]
            except:
                print(f"{self.camera_dict} -- {camera_type_dict}")
                raise ValueError
            curr_cam_kwargs = self.camera_kwargs.get(cam_type, {})
            self.camera_dict[cam_id].set_reading_parameters(**curr_cam_kwargs)

            timestamp = timestamp_dict.get(cam_id + "_frame_received", None)
            if index is not None:
                self.camera_dict[cam_id].set_frame_index(index)

            data_dict = self.camera_dict[cam_id].read_camera(correct_timestamp=timestamp)

            # Process Returned Data #
            if data_dict is None:
                return None
            for key in data_dict:
                full_obs_dict[key].update(data_dict[key])

        return full_obs_dict



def get_hdf5_length(hdf5_file, keys_to_ignore=[]):
    length = None

    for key in hdf5_file.keys():
        if key in keys_to_ignore:
            continue

        curr_data = hdf5_file[key]
        if isinstance(curr_data, h5py.Group):
            curr_length = get_hdf5_length(curr_data, keys_to_ignore=keys_to_ignore)
        elif isinstance(curr_data, h5py.Dataset):
            curr_length = len(curr_data)
        else:
            raise ValueError

        if length is None:
            length = curr_length
        assert curr_length == length

    return length


def load_hdf5_to_dict(hdf5_file, index, keys_to_ignore=[]):
    data_dict = {}

    for key in hdf5_file.keys():
        if key in keys_to_ignore:
            continue

        curr_data = hdf5_file[key]
        if isinstance(curr_data, h5py.Group):
            data_dict[key] = load_hdf5_to_dict(curr_data, index, keys_to_ignore=keys_to_ignore)
        elif isinstance(curr_data, h5py.Dataset):
            data_dict[key] = curr_data[index]
        else:
            raise ValueError

    return data_dict



class TrajectoryReader:
    def __init__(self, filepath, read_images=True):
        self._hdf5_file = h5py.File(filepath, "r")
        is_video_folder = "observations/videos" in self._hdf5_file
        self._read_images = read_images and is_video_folder
        self._length = get_hdf5_length(self._hdf5_file)
        self._video_readers = {}
        self._index = 0

    def length(self):
        return self._length

    def read_timestep(self, index=None, keys_to_ignore=[]):
        # Make Sure We Read Within Range #
        if index is None:
            index = self._index
        else:
            assert not self._read_images
            self._index = index
        assert index < self._length

        # Load Low Dimensional Data #
        keys_to_ignore = [*keys_to_ignore.copy(), "videos"]
        timestep = load_hdf5_to_dict(self._hdf5_file, self._index, keys_to_ignore=keys_to_ignore)

        # Load High Dimensional Data #
        if self._read_images:
            camera_obs = self._uncompress_images()
            timestep["observations"]["image"] = camera_obs

        # Increment Read Index #
        self._index += 1

        # Return Timestep #
        return timestep

    def _uncompress_images(self):
        # WARNING: THIS FUNCTION HAS NOT BEEN TESTED. UNDEFINED BEHAVIOR FOR FAILED READING. #
        video_folder = self._hdf5_file["observations/videos"]
        camera_obs = {}

        for video_id in video_folder:
            # Create Video Reader If One Hasn't Been Made #
            if video_id not in self._video_readers:
                serialized_video = video_folder[video_id]
                filename = create_video_file(byte_contents=serialized_video)
                self._video_readers[video_id] = imageio.get_reader(filename)

            # Read Next Frame #
            camera_obs[video_id] = yield self._video_readers[video_id]
            # Future Note: Could Make Thread For Each Image Reader

        # Return Camera Observation #
        return camera_obs

    def close(self):
        self._hdf5_file.close()


def crawler(dirname, filter_func=None):
    subfolders = [f.path for f in os.scandir(dirname) if f.is_dir()]
    traj_files = [f.path for f in os.scandir(dirname) if (f.is_file() and "trajectory.h5" in f.path)]

    if len(traj_files):
        # Only Save Desired Data #
        if filter_func is None:
            use_data = True
        else:
            hdf5_file = h5py.File(traj_files[0], "r")
            use_data = filter_func(hdf5_file.attrs)
            hdf5_file.close()

        if use_data:
            return [dirname]

    all_folderpaths = []
    for child_dirname in subfolders:
        #if 'ILIAD' not in child_dirname: continue
        child_paths = crawler(child_dirname, filter_func=filter_func)
        all_folderpaths.extend(child_paths)

    return all_folderpaths


def load_trajectory(
    filepath=None,
    read_cameras=True,
    recording_folderpath=None,
    camera_kwargs={},
    remove_skipped_steps=False,
    num_samples_per_traj=None,
    num_samples_per_traj_coeff=1.5,
):
    read_hdf5_images = read_cameras and (recording_folderpath is None)
    read_recording_folderpath = read_cameras and (recording_folderpath is not None)

    traj_reader = TrajectoryReader(filepath, read_images=read_hdf5_images)
    if read_recording_folderpath:
        camera_reader = RecordedMultiCameraWrapper(recording_folderpath, camera_kwargs)

    horizon = traj_reader.length()
    timestep_list = []

    # Choose Timesteps To Save #
    if num_samples_per_traj:
        num_to_save = num_samples_per_traj
        if remove_skipped_steps:
            num_to_save = int(num_to_save * num_samples_per_traj_coeff)
        max_size = min(num_to_save, horizon)
        indices_to_save = np.sort(np.random.choice(horizon, size=max_size, replace=False))
    else:
        indices_to_save = np.arange(horizon)

    # Iterate Over Trajectory #
    for i in indices_to_save:
        # Get HDF5 Data #
        timestep = traj_reader.read_timestep(index=i)

        # If Applicable, Get Recorded Data #
        if read_recording_folderpath:
            timestamp_dict = timestep["observation"]["timestamp"]["cameras"]
            camera_type_dict = {
                k: camera_type_to_string_dict[v] for k, v in timestep["observation"]["camera_type"].items()
            }
            camera_obs = camera_reader.read_cameras(
                index=i, camera_type_dict=camera_type_dict, timestamp_dict=timestamp_dict
            )
            camera_failed = camera_obs is None

            # Add Data To Timestep If Successful #
            if camera_failed:
                break
            else:
                timestep["observation"].update(camera_obs)
        
        # Filter Steps #
        step_skipped = not timestep["observation"]["controller_info"].get("movement_enabled", True)
        delete_skipped_step = step_skipped and remove_skipped_steps

        # Save Filtered Timesteps #
        if delete_skipped_step:
            del timestep
        else:
            timestep_list.append(timestep)

    # Remove Extra Transitions #
    timestep_list = np.array(timestep_list)
    if (num_samples_per_traj is not None) and (len(timestep_list) > num_samples_per_traj):
        ind_to_keep = np.random.choice(len(timestep_list), size=num_samples_per_traj, replace=False)
        timestep_list = timestep_list[ind_to_keep]

    # Close Readers #
    traj_reader.close()

    # Return Data #
    return timestep_list


def _generate_examples(paths) -> Iterator[Tuple[str, Any]]:

    def _resize_and_encode(image, size):
        image = Image.fromarray(image)
        return np.array(image.resize(size, resample=Image.BICUBIC))

    def _parse_example(episode_path):
        FRAMESKIP = 1
        IMAGE_SIZE = (320, 180)

        h5_filepath = os.path.join(episode_path, 'trajectory.h5')
        recording_folderpath = os.path.join(episode_path, 'recordings', 'MP4')

        try:
            traj = load_trajectory(h5_filepath, recording_folderpath=recording_folderpath)
        except:
           print(f"Skipping trajectory {episode_path}.")
           return None
        data = traj[::FRAMESKIP]

        # get language instructions if available
        try:
            metadata_file = glob.glob(episode_path + "/metadata_*.json")[0]
            traj_id = metadata_file[:-5].split('/')[-1].split('_')[-1] #+ '.mp4'
            if traj_id in language_annotations:
                lang_1 = language_annotations[traj_id].get("language_instruction1", "").rstrip()
                lang_2 = language_annotations[traj_id].get("language_instruction2", "").rstrip()
                lang_3 = language_annotations[traj_id].get("language_instruction3", "").rstrip()
                #lang_1, lang_2, lang_3 = get_language_annotations(traj_id)
                #lang_e_1 = language_annotation_embeddings[traj_id]['language_embedding']
                #lang_e_2 = language_annotation_embeddings[traj_id]['language_embedding_2']
                #lang_e_3 = language_annotation_embeddings[traj_id]['language_embedding_3']
            else:
                lang_1 = ''
                lang_2 = ''
                lang_3 = ''
                #lang_e_1 = dummy_language_embedding
                #lang_e_2 = dummy_language_embedding
                #lang_e_3 = dummy_language_embedding
        except:
           print(f"Skipping trajectory {episode_path}.")
           return None

        # if "food_bowl_in_out" in episode_path:
        #     lang_1 = lang_2 = lang_3 = "put the food in the bowl"
        # elif "food_microwave_in_out" in episode_path:
        #     lang_1 = lang_2 = lang_3 = "put the food in the microwave"
        # elif "microwave_open_close" in episode_path:
        #     lang_1 = lang_2 = lang_3 = "open and close the microwave"
        # elif "press_toaster" in episode_path:
        #     lang_1 = lang_2 = lang_3 = "press the toaster"
        # elif "wipe_microwave" in episode_path:
        #     lang_1 = lang_2 = lang_3 = "wipe the microwave"
        # lang_e_1 = lang_e_2 = lang_e_3 = dummy_language_embedding

        try:
            assert all(t.keys() == data[0].keys() for t in data)
            for t in range(len(data)):
                for key in data[0]['observation']['image'].keys():
                    data[t]['observation']['image'][key] = _resize_and_encode(
                        data[t]['observation']['image'][key], IMAGE_SIZE
                    )

            # assemble episode --> here we're assuming demos so we set reward to 1 at the end
            episode = []
        
            for i, step in enumerate(data):
                obs = step['observation']
                action = step['action']
                camera_type_dict = obs['camera_type']
                wrist_ids = [k for k, v in camera_type_dict.items() if v == 0]
                exterior_ids = [k for k, v in camera_type_dict.items() if v != 0]

                episode.append({
                    'observation': {
                        'exterior_image_1_left': obs['image'][f'{exterior_ids[0]}'][..., ::-1],
                        # 'exterior_image_1_right': obs['image'][f'{exterior_ids[0]}_right'][..., ::-1],
                        'exterior_image_2_left': obs['image'][f'{exterior_ids[1]}'][..., ::-1],
                        # 'exterior_image_2_right': obs['image'][f'{exterior_ids[1]}_right'][..., ::-1],
                        'wrist_image_left': obs['image'][f'{wrist_ids[0]}'][..., ::-1],
                        # 'wrist_image_right': obs['image'][f'{wrist_ids[0]}_right'][..., ::-1],
                        'cartesian_position': obs['robot_state']['cartesian_position'],
                        'joint_position': obs['robot_state']['joint_positions'],
                        'gripper_position': np.array([obs['robot_state']['gripper_position']]),
                    },
                    'action_dict': {
                        'cartesian_position': action['cartesian_position'],
                        'cartesian_velocity': action['cartesian_velocity'],
                        'gripper_position': np.array([action['gripper_position']]),
                        'gripper_velocity': np.array([action['gripper_velocity']]),
                        'joint_position': action['joint_position'],
                        'joint_velocity': action['joint_velocity'],
                    },
                    'action': np.concatenate((action['cartesian_position'], [action['gripper_position']])),
                    'discount': 1.0,
                    'reward': float((i == (len(data) - 1) and 'success' in episode_path)),
                    'is_first': i == 0,
                    'is_last': i == (len(data) - 1),
                    'is_terminal': i == (len(data) - 1),
                    'language_instruction': lang_1,
                    'language_instruction_2': lang_2,
                    'language_instruction_3': lang_3,
                    #'language_embedding': lang_e_1,
                    #'language_embedding_2': lang_e_2,
                    #'language_embedding_3': lang_e_3,
                })
        except:
           print(f"Skipping trajectory {episode_path}.")
           return None

        # create output data sample
        sample = {
            'steps': episode,
            'episode_metadata': {
                'file_path': h5_filepath,
                'recording_folderpath': recording_folderpath
            }
        }
        # if you want to skip an example for whatever reason, simply return None
        return episode_path, sample

    # for smallish datasets, use single-thread parsing
    for sample in paths:
       yield _parse_example(sample)


class Droid(tfds.core.GeneratorBasedBuilder):
    """DatasetBuilder for example dataset."""

    VERSION = tfds.core.Version('1.0.0')
    RELEASE_NOTES = {
      '1.0.0': 'Initial release.',
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def _info(self) -> tfds.core.DatasetInfo:
        """Dataset metadata (homepage, citation,...)."""
        return self.dataset_info_from_configs(
            features=tfds.features.FeaturesDict({
            'steps': tfds.features.Dataset({
                    'observation': tfds.features.FeaturesDict({
                        'exterior_image_1_left': tfds.features.Image(
                            shape=(180, 320, 3),
                            dtype=np.uint8,
                            encoding_format='jpeg',
                            doc='Exterior camera 1 left viewpoint',
                        ),
                        # 'exterior_image_1_right': tfds.features.Image(
                        #     shape=(180, 320, 3),
                        #     dtype=np.uint8,
                        #     encoding_format='jpeg',
                        #     doc='Exterior camera 1 right viewpoint'
                        # ),
                        'exterior_image_2_left': tfds.features.Image(
                            shape=(180, 320, 3),
                            dtype=np.uint8,
                            encoding_format='jpeg',
                            doc='Exterior camera 2 left viewpoint'
                        ),
                        # 'exterior_image_2_right': tfds.features.Image(
                        #     shape=(180, 320, 3),
                        #     dtype=np.uint8,
                        #     encoding_format='jpeg',
                        #     doc='Exterior camera 2 right viewpoint'
                        # ),
                        'wrist_image_left': tfds.features.Image(
                            shape=(180, 320, 3),
                            dtype=np.uint8,
                            encoding_format='jpeg',
                            doc='Wrist camera RGB left viewpoint',
                        ),
                        # 'wrist_image_right': tfds.features.Image(
                        #     shape=(180, 320, 3),
                        #     dtype=np.uint8,
                        #     encoding_format='jpeg',
                        #     doc='Wrist camera RGB right viewpoint'
                        # ),
                        'cartesian_position': tfds.features.Tensor(
                            shape=(6,),
                            dtype=np.float64,
                            doc='Robot Cartesian state',
                        ),
                        'gripper_position': tfds.features.Tensor(
                            shape=(1,),
                            dtype=np.float64,
                            doc='Gripper position statae',
                        ),
                        'joint_position': tfds.features.Tensor(
                            shape=(7,),
                            dtype=np.float64,
                            doc='Joint position state'
                        )
                    }),
                    'action_dict': tfds.features.FeaturesDict({
                        'cartesian_position': tfds.features.Tensor(
                            shape=(6,),
                            dtype=np.float64,
                            doc='Commanded Cartesian position'
                        ),
                        'cartesian_velocity': tfds.features.Tensor(
                            shape=(6,),
                            dtype=np.float64,
                            doc='Commanded Cartesian velocity'
                        ),
                        'gripper_position': tfds.features.Tensor(
                            shape=(1,),
                            dtype=np.float64,
                            doc='Commanded gripper position'
                        ),
                        'gripper_velocity': tfds.features.Tensor(
                            shape=(1,),
                            dtype=np.float64,
                            doc='Commanded gripper velocity'
                        ),
                        'joint_position': tfds.features.Tensor(
                            shape=(7,),
                            dtype=np.float64,
                            doc='Commanded joint position'
                        ),
                        'joint_velocity': tfds.features.Tensor(
                            shape=(7,),
                            dtype=np.float64,
                            doc='Commanded joint velocity'
                        )
                    }),
                    'action': tfds.features.Tensor(
                        shape=(7,),
                        dtype=np.float64,
                        doc='Robot action, consists of [6x joint velocities, \
                            1x gripper position].',
                    ),
                    'discount': tfds.features.Scalar(
                        dtype=np.float32,
                        doc='Discount if provided, default to 1.'
                    ),
                    'reward': tfds.features.Scalar(
                        dtype=np.float32,
                        doc='Reward if provided, 1 on final step for demos.'
                    ),
                    'is_first': tfds.features.Scalar(
                        dtype=np.bool_,
                        doc='True on first step of the episode.'
                    ),
                    'is_last': tfds.features.Scalar(
                        dtype=np.bool_,
                        doc='True on last step of the episode.'
                    ),
                    'is_terminal': tfds.features.Scalar(
                        dtype=np.bool_,
                        doc='True on last step of the episode if it is a terminal step, True for demos.'
                    ),
                    'language_instruction': tfds.features.Text(
                        doc='Language Instruction.'
                    ),
                    'language_instruction_2': tfds.features.Text(
                        doc='Alternative Language Instruction.'
                    ),
                    'language_instruction_3': tfds.features.Text(
                        doc='Alternative Language Instruction.'
                    ),
                    #'language_embedding': tfds.features.Tensor(
                    #    shape=(512,),
                    #    dtype=np.float32,
                    #    doc='Kona language embedding. '
                    #        'See https://tfhub.dev/google/universal-sentence-encoder-large/5'
                    #),
                    #'language_embedding_2': tfds.features.Tensor(
                    #    shape=(512,),
                    #    dtype=np.float32,
                    #    doc='Alternative Kona language embedding.'
                    #),
                    #'language_embedding_3': tfds.features.Tensor(
                    #    shape=(512,),
                    #    dtype=np.float32,
                    #    doc='Alternative Kona language embedding.'
                    #),
                }),
                'episode_metadata': tfds.features.FeaturesDict({
                    'file_path': tfds.features.Text(
                        doc='Path to the original data file.'
                    ),
                    'recording_folderpath': tfds.features.Text(
                        doc='Path to the folder of recordings.'
                    )
                }),
            }))

    def _split_paths(self):
        """Define data splits."""
        # create list of all examples
        print("Crawling all episode paths...")
        episode_paths = crawler('/nfs/kun2/datasets/r2d2/r2d2-data-full')
        # episode_paths = crawler('/nfs/kun2/datasets/r2d2/r2d2_iris_finetune')
        print(f"Found {len(episode_paths)} candidates.")
        episode_paths = [p for p in episode_paths if os.path.exists(p + '/trajectory.h5') and \
                         os.path.exists(p + '/recordings/MP4')]
        random.shuffle(episode_paths)
        print(f"Found {len(episode_paths)} episodes!")
        #from collections import Counter
        #cc = Counter(episode_paths)
        #breakpoint()
        return {
            'train': episode_paths,
        }

    def _split_generators(self, dl_manager: tfds.download.DownloadManager):
        """Define data splits."""
        split_paths = self._split_paths()
        return {split: _generate_examples(paths=split_paths[split]) for split in split_paths}

    def _generate_examples(self):
        pass  # this is implemented in global method to enable multiprocessing

    def _download_and_prepare(  # pytype: disable=signature-mismatch  # overriding-parameter-type-checks
            self,
            dl_manager: download.DownloadManager,
            download_config: download.DownloadConfig,
    ) -> None:
        """Generate all splits and returns the computed split infos."""
        split_builder = ParallelSplitBuilder(
            split_dict=self.info.splits,
            features=self.info.features,
            dataset_size=self.info.dataset_size,
            max_examples_per_split=download_config.max_examples_per_split,
            beam_options=download_config.beam_options,
            beam_runner=download_config.beam_runner,
            file_format=self.info.file_format,
            shard_config=download_config.get_shard_config(),
            split_paths=self._split_paths(),
            parse_function=self._generate_examples,
        )
        split_generators = self._split_generators(dl_manager)
        split_generators = split_builder.normalize_legacy_split_generators(
            split_generators=split_generators,
            generator_fn=self._generate_examples,
            is_beam=False,
        )
        dataset_builder._check_split_names(split_generators.keys())

        # Start generating data for all splits
        path_suffix = file_adapters.ADAPTER_FOR_FORMAT[
            self.info.file_format
        ].FILE_SUFFIX

        split_info_futures = []
        for split_name, generator in utils.tqdm(
                split_generators.items(),
                desc="Generating splits...",
                unit=" splits",
                leave=False,
        ):
            filename_template = naming.ShardedFileTemplate(
                split=split_name,
                dataset_name=self.name,
                data_dir=self.data_path,
                filetype_suffix=path_suffix,
            )
            future = split_builder.submit_split_generation(
                split_name=split_name,
                generator=generator,
                filename_template=filename_template,
                disable_shuffling=self.info.disable_shuffling,
            )
            split_info_futures.append(future)

        # Finalize the splits (after apache beam completed, if it was used)
        split_infos = [future.result() for future in split_info_futures]

        # Update the info object with the splits.
        split_dict = splits_lib.SplitDict(split_infos)
        self.info.set_splits(split_dict)


class _SplitInfoFuture:
    """Future containing the `tfds.core.SplitInfo` result."""

    def __init__(self, callback: Callable[[], splits_lib.SplitInfo]):
        self._callback = callback

    def result(self) -> splits_lib.SplitInfo:
        return self._callback()


def parse_examples_from_generator(paths, split_name, total_num_examples, features, serializer):
    generator = _generate_examples(paths)
    outputs = []
    for sample in utils.tqdm(
            generator,
            desc=f'Generating {split_name} examples...',
            unit=' examples',
            total=total_num_examples,
            leave=False,
            mininterval=1.0,
    ):
        if sample is None: continue
        key, example = sample
        try:
            example = features.encode_example(example)
        except Exception as e:  # pylint: disable=broad-except
            utils.reraise(e, prefix=f'Failed to encode example:\n{example}\n')
        outputs.append((key, serializer.serialize_example(example)))
    return outputs


class ParallelSplitBuilder(split_builder_lib.SplitBuilder):
    def __init__(self, *args, split_paths, parse_function, **kwargs):
        super().__init__(*args, **kwargs)
        self._split_paths = split_paths
        self._parse_function = parse_function

    def _build_from_generator(
            self,
            split_name: str,
            generator: Iterable[KeyExample],
            filename_template: naming.ShardedFileTemplate,
            disable_shuffling: bool,
    ) -> _SplitInfoFuture:
        """Split generator for example generators.

        Args:
          split_name: str,
          generator: Iterable[KeyExample],
          filename_template: Template to format the filename for a shard.
          disable_shuffling: Specifies whether to shuffle the examples,

        Returns:
          future: The future containing the `tfds.core.SplitInfo`.
        """
        total_num_examples = None
        serialized_info = self._features.get_serialized_info()
        writer = ResumeWriter(
            serializer=example_serializer.ExampleSerializer(serialized_info),
            filename_template=filename_template,
            hash_salt=split_name,
            disable_shuffling=disable_shuffling,
            file_format=self._file_format,
            shard_config=self._shard_config,
            resume_dir=RESUME_DIR,
        )

        del generator  # use parallel generators instead
        paths = self._split_paths[split_name]
        path_lists = chunk_max(paths, N_WORKERS, MAX_PATHS_IN_MEMORY)  # generate N file lists
        print(f"Generating with {N_WORKERS} workers!")
        pool = Pool(processes=N_WORKERS)
        for i in range(START_CHUNK, len(path_lists)):
            paths = path_lists[i]
            print(f"Processing chunk {i + 1} of {len(path_lists)}.")
            results = pool.map(
                partial(
                    parse_examples_from_generator,
                    split_name=split_name,
                    total_num_examples=total_num_examples,
                    serializer=writer._serializer,
                    features=self._features
                ),
                paths
            )
            # write results to shuffler --> this will automatically offload to disk if necessary
            print("Writing conversion results...")
            for result in itertools.chain(*results):
                key, serialized_example = result
                try:
                    writer._shuffler.add(key, serialized_example)
                    writer._num_examples += 1
                except:
                    print(f"Failed to write episode {key}")
                    continue
        pool.close()

        print("Finishing split conversion...")
        shard_lengths, total_size = writer.finalize()

        split_info = splits_lib.SplitInfo(
            name=split_name,
            shard_lengths=shard_lengths,
            num_bytes=total_size,
            filename_template=filename_template,
        )
        return _SplitInfoFuture(lambda: split_info)

def dictlist2listdict(DL):
    " Converts a dict of lists to a list of dicts "
    return [dict(zip(DL, t)) for t in zip(*DL.values())]

def chunks(l, n):
    """Yield n number of sequential chunks from l."""
    d, r = divmod(len(l), n)
    for i in range(n):
        si = (d + 1) * (i if i < r else r) + d * (0 if i < r else i - r)
        yield l[si:si + (d + 1 if i < r else d)]

def chunk_max(l, n, max_chunk_sum):
    out = []
    for _ in range(int(np.ceil(len(l) / max_chunk_sum))):
        out.append(list(chunks(l[:max_chunk_sum], n)))
        l = l[max_chunk_sum:]
    return out
