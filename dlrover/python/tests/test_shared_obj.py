# Copyright 2023 The DLRover Authors. All rights reserved.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import pickle
import unittest

from dlrover.python.common.shared_obj import (
    SharedDict,
    SharedLock,
    SharedMemory,
    SharedQueue,
)


class SharedLockTest(unittest.TestCase):
    def test_shared_lock(self):
        name = "test"
        server_lock = SharedLock(name, create=True)
        client_lock = SharedLock(name, create=False)
        acquired = server_lock.acquire()
        self.assertTrue(acquired)
        acquired = client_lock.acquire(blocking=False)
        self.assertFalse(acquired)
        server_lock.release()
        acquired = client_lock.acquire(blocking=False)
        self.assertTrue(acquired)
        client_lock.release()

    def test_shared_queue(self):
        name = "test"
        server_queue = SharedQueue(name, create=True)
        client_queue = SharedQueue(name, create=False)
        server_queue.put(2)
        qsize = server_queue.qsize()
        self.assertEqual(qsize, 1)
        value = server_queue.get()
        self.assertEqual(value, 2)
        client_queue.put(3)
        qsize = client_queue.qsize()
        self.assertEqual(qsize, 1)
        qsize = client_queue.qsize()
        self.assertEqual(qsize, 1)
        value = client_queue.get()
        self.assertEqual(value, 3)

    def test_shared_dict(self):
        name = "test"
        read_dict = SharedDict(name=name, recv=True)
        write_dict = SharedDict(name=name, recv=False)
        new_dict = {"a": 1, "b": 2}
        write_dict.update(new_dict=new_dict)
        new_dict["a"] = 4
        write_dict.update(new_dict=new_dict)
        d = read_dict.get()
        self.assertDictEqual(d, new_dict)
        with open(read_dict._local_saving_file, "rb") as f:
            store_d = pickle.load(f)
            self.assertDictEqual(store_d, new_dict)


class SharedMemoryTest(unittest.TestCase):
    def test_unlink(self):
        fanme = "test-shm"
        with self.assertRaises(ValueError):
            shm = SharedMemory(name=fanme, create=True, size=-1)
        with self.assertRaises(ValueError):
            shm = SharedMemory(name=fanme, create=True, size=0)
        shm = SharedMemory(name=fanme, create=True, size=1024)
        shm.buf[0:4] = b"abcd"
        shm.close()
        shm.unlink()
        with self.assertRaises(FileNotFoundError):
            shm = SharedMemory(name=fanme, create=False)
