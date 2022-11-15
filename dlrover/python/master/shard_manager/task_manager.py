import threading
import time
import inspect

from collections import OrderedDict
from typing import Dict

from dlrover.proto import elastic_training_pb2
from dlrover.python.common.log_utils import default_logger as logger
from dlrover.python.master.shard_manager.batch_dataset_manager import (
    BatchDatasetManager,
    DoingTask
)
from dlrover.python.master.shard_manager.base_dataset_manager import (
    DatasetManger,
    DatasetShardCheckpoint,
)
from dlrover.python.master.shard_manager.dataset_splitter import (
    DatasetSplitterFactory
)
from dlrover.python.master.monitor.speed_monitor import (
    SpeedMonitor
)

_TASK_TIMEOUT_THRESHOLD_SECS = 1800
_DEFAULT_NUM_MINIBATCHES_PER_SHARD = 100


class TaskManager(object):
    """Creates and dispatches Tasks. Keep track of a Task's lifecycle."""

    def __init__(
        self, relaunch_timeout_worker: bool
    ):
        """
        Args:
            relaunch_timeout_worker: Whether to relaunch a worker
                when it does not report a task status for a long time.
        """
        self._lock = threading.Lock()
        self.relaunch_timeout_worker = relaunch_timeout_worker
        self._should_stop = False
        self._datasets: Dict[str, DatasetManger] = OrderedDict()
        self._worker_start_task_time = {}
        self._task_timeout_callbacks = []
        self._speed_monitor = SpeedMonitor()

    def new_dataset(
        self,
        batch_size,
        num_epochs,
        dataset_size,
        shuffle,
        num_minibatches_per_shard,
        dataset_name=None,
        task_type=elastic_training_pb2.NONE,
        storage_type=None
    ):
        frame = inspect.currentframe()
        args, _, _, values = inspect.getargvalues(frame)
        logger.info(
            "Set dataset sharding parameters: %s", 
            [(i, values[i]) for i in args],
        )

        with self._lock:
            if dataset_name in self._datasets:
                logger.info(
                    "The shards for dataset %s have already been initialized. "
                    "Ignore these shard parameters.",
                    dataset_name,
                )
                return
            if dataset_size < 0:
                logger.error(
                    "No shard for datset %s because dataset size %s <= 0",
                    dataset_name,
                    dataset_size,
                )
                return
            num_minibatches_per_task = (
                num_minibatches_per_shard or _DEFAULT_NUM_MINIBATCHES_PER_SHARD
            )
            shard_size = batch_size * num_minibatches_per_task
            dataset_splitter = DatasetSplitterFactory.create_dataset_splitter(
                shuffle,
                shard_size,
                dataset_size,
                num_epochs,
                dataset_name,
                storage_type,
            )
            dataset = BatchDatasetManager(
                task_type=task_type,
                batch_size=batch_size,
                dataset_splitter=dataset_splitter,
            )
            self._datasets[dataset_name] = dataset

    def get_dataset_task(self, worker_id, dataset_name):
        """Return next Task"""
        with self._lock:
            dataset = self._datasets.get(dataset_name, None)
            if dataset:
                task = dataset.get_task(worker_id)
                if (
                    task.task_type == elastic_training_pb2.EVALUATION
                ):
                    # All workers will stop training to evaluate the model
                    # at parallel validation
                    self._speed_monitor.reset_running_speed_monitor()
                self._worker_start_task_time[worker_id] = time.time()
                return task
            else:
                return None

    def get_dataset(self, dataset_name):
        return self._datasets.get(dataset_name, None)

    def report_dataset_task(self, request, success):
        """Report if the task is successful or not"""

        task_id = request.task_id
        dataset_name = request.dataset_name
        with self._lock:
            dataset = self._datasets.get(dataset_name, None)
            if not dataset:
                raise ValueError(
                    "There is no dataset shard for the dataset {}".format(
                        dataset_name
                    )
                )
            success, doing_task = dataset.report_task_status(
                task_id, success
            )
            self._worker_start_task_time[doing_task.worker_id] = time.time()
            return doing_task.task, doing_task.worker_id

    def finished(self):
        """Return if all tasks are done"""
        if not self._datasets:
            return False
        finished = all([ds.completed() for _, ds in self._datasets.items()])
        return finished

    def recover_tasks(self, worker_id):
        """Recover doing tasks for a dead worker if needed"""
        for name, dataset in self._datasets.items():
            doing_tasks: Dict[int, DoingTask] = dataset.get_doing_tasks()
            ids = [
                task_id
                for task_id, doing_task in doing_tasks.items()
                if doing_task.worker_id == worker_id
            ]
            request = elastic_training_pb2.ReportTaskResultRequest()
            for id in ids:
                request.task_id = id
                request.dataset_name = name
                self.report_dataset_task(request, False)
            logger.info("Recover tasks assigned to worker %d" % worker_id)

    def start(self):
        if self.relaunch_timeout_worker:
            threading.Thread(
                target=self._check_and_reassign_timeout_tasks,
                name="check_timeout_tasks",
                daemon=True,
            ).start()

    def reset_worker_start_task_time(self, worker_id):
        self._worker_start_task_time[worker_id] = time.time()

    def set_task_timeout_callback(self, callback_fn):
        self._task_timeout_callbacks.append(callback_fn)

    def _invoke_task_timeout_callback(self, worker_id):
        for callback_fn in self._task_timeout_callbacks:
            callback_fn(worker_id)

    def _check_and_reassign_timeout_tasks(self):
        """Check whether there are timeout tasks periodically.
        """
        logger.info("Start the thread to monitor timeout tasks")
        while True:
            for _, dataset in self._datasets.items():
                doing_tasks = dataset.doing.copy()
                cur = time.time()
                for task_id, doing_task in doing_tasks.items():
                    start = self._worker_start_task_time.get(
                        doing_task.worker_id, cur
                    )
                    if (
                        doing_task.task.type == elastic_training_pb2.EVALUATION
                        and cur - start > _TASK_TIMEOUT_THRESHOLD_SECS
                    ):
                        logger.info(
                            "worker %d timeout with task %d, relaunch it",
                            doing_task.worker_id,
                            task_id,
                        )
                        self._invoke_task_timeout_callback(
                            doing_task.worker_id
                        )
                        break
            time.sleep(30)

    def get_dataset_checkpoint(self, dataset_name):
        """Get the data shard checkpoint by dataset name.

        Args:
            dataset_name: string

        Returns:
            DatasetShardCheckpoint.
        """
        with self._lock:
            if dataset_name in self._datasets:
                dataset = self._datasets[dataset_name]
                return dataset.checkpoint()
            else:
                return None

    def restore_dataset_from_checkpoint(self, checkpoint):
        # try:
        dataset_checkpoint = DatasetShardCheckpoint.from_json(checkpoint)
        dataset = self._datasets.get(dataset_checkpoint.dataset_name, None)
        if not dataset:
            logger.error("No dataset for checkpoint %s", checkpoint)

        dataset.restore_checkpoint(dataset_checkpoint)
        logger.info(
            "Restore %s dataset shards from checkpoint %s",
            dataset_checkpoint.dataset_name,
            checkpoint,
        )
        return True
        # except Exception as e:
        #     logger.error("Fail to restore shards from the checkpoint %s", e)

        return False

    def get_dataset_epoch(self, dataset_name):
        if dataset_name in self._datasets:
            return self._datasets[dataset_name].get_epoch()
        else:
            logger.error("There is not exit dataset {}".format(dataset_name))
            return 0

    def training_started(self):
        """The training has started if there is a completed batch"""
        for _, dataset in self._datasets.items():
            if dataset.get_completed_step() > 0:
                return True
        return False

    def remove_running_worker(self, worker_id):
        self._speed_monitor.remove_running_worker(worker_id)
