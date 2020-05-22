# Copyright 2020 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

__author__ = "lizlooney@google.com (Liz Looney)"

# Python Standard Library
import json
import os

# Other Modules
from google.oauth2 import service_account
import googleapiclient.discovery
import googleapiclient.errors

# My Modules
import blob_storage
import constants
import storage
import util

BUCKET = ('%s' % constants.PROJECT_ID)

def start_training_model(team_uuid, dataset_uuid, start_time_ms):
    object_detection_tar_gz = 'gs://%s/static/training/object_detection-0.1.tar.gz' % BUCKET
    slim_tar_gz = 'gs://%s/static/training/slim-0.1.tar.gz' % BUCKET
    pycocotools_tar_gz = 'gs://%s/static/training/pycocotools-2.0.tar.gz' % BUCKET
    fine_tune_checkpoint = 'gs://%s/static/training/models/ssd_mobilenet_v1_0.75_depth_300x300_coco14_sync_2018_07_03/model.ckpt' % BUCKET

    dataset_entity = storage.retrieve_dataset_entity(team_uuid, dataset_uuid)
    model_uuid = storage.model_trainer_starting()

    # Create the pipeline.config file and store it in cloud storage.
    bucket = util.storage_client().get_bucket(BUCKET)
    config_template_blob_name = 'static/training/models/configs/ssd_mobilenet_v1_0.75_depth_quantized_300x300_pets_sync.config'
    pipeline_config = (bucket.blob(config_template_blob_name).download_as_string().decode('utf-8')
        .replace('TO_BE_CONFIGURED/num_classes', str(len(dataset_entity['sorted_label_list'])))
        .replace('TO_BE_CONFIGURED/fine_tune_checkpoint', fine_tune_checkpoint)
        .replace('TO_BE_CONFIGURED/train_input_path', dataset_entity['train_input_path'])
        .replace('TO_BE_CONFIGURED/label_map_path', dataset_entity['label_map_path'])
        .replace('TO_BE_CONFIGURED/eval_input_path', dataset_entity['eval_input_path'])
        .replace('TO_BE_CONFIGURED/num_examples', str(dataset_entity['eval_frame_count']))
        )
    pipeline_config_path = blob_storage.store_pipeline_config(team_uuid, model_uuid, pipeline_config)

    model_dir = blob_storage.get_model_folder_path(team_uuid, model_uuid)
    job_dir = model_dir
    checkpoint_dir = model_dir

    ml = __get_ml_service()
    parent = __get_parent()
    train_job_id = __get_train_job_id(model_uuid)
    scheduling = {
        # TODO(lizlooney): Adjust maxRunningTime.
        'maxRunningTime': '3600s', # 1 hour
    }
    train_training_input = {
        'scaleTier': 'BASIC_TPU',
        'packageUris': [
            object_detection_tar_gz,
            slim_tar_gz,
            pycocotools_tar_gz,
        ],
        'pythonModule': 'object_detection.model_tpu_main',
        'args': [
            '--model_dir', model_dir,
            '--tpu_zone', 'us-central1',
            '--pipeline_config_path', pipeline_config_path,
        ],
        # TODO(lizlooney): Specify hyperparameters.
        #'hyperparameters': {
        #  object (HyperparameterSpec)
        #},
        'region': 'us-central1', # Don't hardcode?
        'jobDir': job_dir,
        'runtimeVersion': '1.15',
        'pythonVersion': '3.7',
        'scheduling': scheduling,
    }
    train_job = {
        'jobId': train_job_id,
        'trainingInput': train_training_input,
    }
    train_job_response = ml.projects().jobs().create(parent=parent, body=train_job).execute()
    if dataset_entity['eval_record_count'] > 0:
        eval_job_id = __get_eval_job_id(model_uuid)
        eval_training_input = {
            'scaleTier': 'BASIC_GPU',
            'packageUris': [
                object_detection_tar_gz,
                slim_tar_gz,
                pycocotools_tar_gz,
            ],
            'pythonModule': 'object_detection.model_main',
            'args': [
                '--model_dir', model_dir,
                '--pipeline_config_path', pipeline_config_path,
                '--checkpoint_dir', checkpoint_dir,
            ],
            # TODO(lizlooney): Specify hyperparameters.
            #'hyperparameters': {
            #  object (HyperparameterSpec)
            #},
            'region': 'us-central1',
            'jobDir': job_dir,
            'runtimeVersion': '1.15',
            'pythonVersion': '3.7',
            'scheduling': scheduling,
        }
        eval_job = {
            'jobId': eval_job_id,
            'trainingInput': eval_training_input,
        }
        eval_job_response = ml.projects().jobs().create(parent=parent, body=eval_job).execute()
    else:
        eval_job_response = None
    model_entity = storage.model_trainer_started(team_uuid, model_uuid, start_time_ms,
        dataset_uuid, dataset_entity['video_filenames'], fine_tune_checkpoint,
        train_job_response, eval_job_response)
    return model_entity


def retrieve_model_entity(team_uuid, model_uuid):
    model_entity = storage.retrieve_model_entity(team_uuid, model_uuid)
    # If the train and eval jobs weren't done last time we checked, check now.
    if __is_not_done(model_entity['train_job_state']) or __is_not_done(model_entity['eval_job_state']):
        ml = __get_ml_service()
        train_job_name = __get_train_job_name(model_uuid)
        train_job_response = ml.projects().jobs().get(name=train_job_name).execute()
        if model_entity['eval_job']:
            eval_job_name = __get_eval_job_name(model_uuid)
            eval_job_response = ml.projects().jobs().get(name=eval_job_name).execute()
            # If the train job has failed or been cancelled, cancel the eval job is it's still alive.
            if __is_dead_or_dying(train_job_response['state']) and __is_alive(eval_job_response['state']):
                ml.projects().jobs().cancel(name=eval_job_name).execute()
                eval_job_response = ml.projects().jobs().get(name=eval_job_name).execute()
        else:
            eval_job_response = None
        model_entity = storage.update_model_entity(team_uuid, model_uuid, train_job_response, eval_job_response)
    return model_entity

def delete_model(team_uuid, model_uuid):
    model_entity = storage.retrieve_model_entity(team_uuid, model_uuid)
    # If the train and eval jobs weren't done last time we checked, we need to check again and
    # cancel the jobs if they are alive.
    if __is_not_done(model_entity['train_job_state']) or __is_not_done(model_entity['eval_job_state']):
        ml = __get_ml_service()
        train_job_name = __get_train_job_name(model_uuid)
        train_job_response = ml.projects().jobs().get(name=train_job_name).execute()
        if __is_alive(train_job_response['state']):
            ml.projects().jobs().cancel(name=train_job_name).execute()
        if model_entity['eval_job']:
            eval_job_name = __get_eval_job_name(model_uuid)
            eval_job_response = ml.projects().jobs().get(name=eval_job_name).execute()
            if __is_alive(eval_job_response['state']):
                ml.projects().jobs().cancel(name=eval_job_name).execute()
    storage.delete_model(team_uuid, model_uuid)

def __get_ml_service():
    scopes = ['https://www.googleapis.com/auth/cloud-platform']
    credentials = service_account.Credentials.from_service_account_file('key.json', scopes=scopes)
    return googleapiclient.discovery.build(
        serviceName='ml', version='v1', credentials=credentials, cache_discovery=False)

def __get_parent():
    # TODO(lizlooney): Is the project id here supposed to be our Google Cloud Project ID?
    return 'projects/%s' % constants.PROJECT_ID

def __get_train_job_id(model_uuid):
    return 'train_%s' % model_uuid

def __get_eval_job_id(model_uuid):
    return 'eval_%s' % model_uuid

def __get_train_job_name(model_uuid):
    return '%s/jobs/%s' % (__get_parent(), __get_train_job_id(model_uuid))

def __get_eval_job_name(model_uuid):
    return '%s/jobs/%s' % (__get_parent(), __get_eval_job_id(model_uuid))

def __is_alive(state):
    return (state == 'QUEUED' or
            state == 'PREPARING' or
            state == 'RUNNING')

def __is_dead_or_dying(state):
    return (state == 'FAILED' or
            state == 'CANCELLING' or
            state == 'CANCELLED')

def __is_not_done(state):
    return (state != 'SUCCEEDED' and
            state != 'FAILED' and
            state != 'CANCELLED')
