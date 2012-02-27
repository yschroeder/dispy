#!/usr/bin/env python

# dispy: Distribute computations among CPUs/cores on a single machine or
# machines in cluster(s), grid, cloud etc. for parallel execution.

# Copyright (C) 2011 Giridhar Pemmasani (pgiri@yahoo.com)

# This file is part of dispy.

# dispy is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# dispy is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.

# You should have received a copy of the GNU Lesser General Public License
# along with dispy.  If not, see <http://www.gnu.org/licenses/>.
                                
import os
import sys
import time
import socket
import inspect
import stat
import cPickle
import threading
import struct
import base64
import logging
import re
import ssl
import hashlib
import traceback
import types
import itertools
import Queue
import shelve
import datetime
import atexit
import platform

from asyncoro import Coro, AsynCoro, CoroLock, CoroCondition, AsynCoroSocket, MetaSingleton

if platform.system() == 'Windows':
    # with Windows XP (at least) shelve does not seem to work: When
    # opened during fault recovery, it bails with 'unsupported hash
    # version 9', so use dumbdbm instead.
    import dumbdbm
    shelve = dumbdbm
    def shelve_repr(s):
        return cPickle.dumps(s)
else:
    def shelve_repr(s):
        return s

_dispy_version = '1.8-coro'

class DispyJob(object):
    """Job scheduled for execution with dispy.

    Once a job is scheduled (with a tuple of arguments), the __call__
    method can be invoked. This will wait until the job is
    complete. The result of the call (either the return value in case
    of python methods or the exit value in case of programs) will be
    returned; the result is also available as .result member if
    needed.  In addition, any output, error, exception messages from
    the job will be available as .stdout, .stderr and .exception
    members. The time when the job was submitted for execution on a
    node will be available as .start_time and when the job results
    became available as .end_time.

    .id field is initially set to None and may be assigned by user to
    any value that is appropriate.  This may be useful, for example,
    to distinguish one job from another.

    .status is read-only field; it is set to one of Created, Running,
    Finished, Cancelled, Terminated and ProvisionalResult, indicating
    current status of job.  If job is created for SharedJobCluster,
    status is not updated to Running when job is actually running.

    .ip_addr is read-inly field; it is set to IP address of node that
    executed job.

    .finish is a read-only event that is set when a job's results are
    available.

    """
    Created = 1
    Running = 2
    ProvisionalResult = 3
    # NB: Cancelled, Terminated and Finished status should have
    # values in that order, as PriorityQueue sorts data.
    # Thus, if a job with provisional result is already in the queue
    # and a job is finished, finished/terminated job is processed (in
    # callback) last.
    Cancelled = 8
    Terminated = 9
    Finished = 10
    def __init__(self):
        # id can be assigned by user as appropriate (e.g., to distinguish jobs)
        self.id = None
        # rest are read-only
        self.result = None
        self.stdout = None
        self.stderr = None
        self.exception = None
        self.start_time = None
        self.end_time = None
        self.status = DispyJob.Created
        self.ip_addr = None
        self.finish = threading.Event()

        # _dispy_job_ is for dispy implementation only - it is opaque to users
        self._dispy_job_ = None

    def __call__(self, clear=False):
        self.finish.wait()
        if clear:
            self.finish.clear()
        return self.result

def _xor_string(data, key):
    """Internal use only.
    """
    if not key:
        return data
    return ''.join(hex(ord(x) ^ ord(y))[2:] for (x, y) in itertools.izip(data, itertools.cycle(key)))

def _node_name_ipaddr(node):
    """Internal use only.
    """
    if not node:
        return (None, None)
    if node.find('*') >= 0:
        return (node, node)
    try:
        socket.inet_pton(socket.AF_INET, node)
        try:
            name = socket.gethostbyaddr(node)[0]
        except:
            name = node
        return (name, node)
    except:
        try:
            ip_addr = socket.gethostbyname(node)
            return (node, ip_addr)
        except:
            return (None, None)

def _parse_nodes(nodes):
    """Internal use only.
    """
    node_spec = {}
    for node in nodes:
        node_port = None
        if isinstance(node, str):
            name, ip_addr = _node_name_ipaddr(node)
            if not ip_addr:
                if node.find('*') >= 0:
                    name, ip_addr = node, node
                else:
                    logging.warning('Node "%s" is invalid; ignoring it.', str(node))
                    continue
            match_re = ip_addr.replace('.', '\\.').replace('*', '.*')
            node_port = None
        elif isinstance(node, tuple):
            name, ip_addr = _node_name_ipaddr(node[0])
            if not ip_addr:
                if node[0].find('*') >= 0:
                    name, ip_addr = node[0], node[0]
                else:
                    logging.warning('Node "%s" is invalid; ignoring it.')
                    continue
            match_re = ip_addr.replace('.', '\\.').replace('*', '.*')
            if len(node) == 2:
                node_port = node[1]
            else:
                logging.warning('Node "%s" is invalid; ignoring it', str(node))
                continue
        else:
            logging.warning('Node "%s" is invalid; ignoring it', str(node))
            continue
        node_spec[match_re] = {'port':node_port, 'ip_addr':ip_addr, 'name':name}
    return node_spec

class _Compute(object):
    """Internal use only.
    """
    func_type = 1
    prog_type = 2
    def __init__(self, compute_type, name):
        assert compute_type == _Compute.func_type or compute_type == _Compute.prog_type
        self.type = compute_type
        self.name = name
        self.id = None
        self.code = None
        self.job_result_port = None
        self.env = None
        self.dest_path = None
        self.xfer_files = []
        self.cleanup = True
        self.auth_code = None
        self.node_ip = None
        self.nodes = {}
        self.node_spec = None
        self.reentrant = False
        self.scheduler_ip_addr = None
        self.scheduler_port = None
        self.pulse_interval = None

    def __getstate__(self):
        state = dict(self.__dict__)
        if 'auth_code' in state:
            del state['auth_code']
            del state['nodes']
        return state

class _XferFile(object):
    """Internal use only.
    """
    def __init__(self, name, stat_buf, compute_id=None):
        self.name = name
        self.stat_buf = stat_buf
        self.compute_id = compute_id

class _Node(object):
    """Internal use only.
    """
    def __init__(self, ip_addr, port, cpus, sign, secret, keyfile=None, certfile=None):
        self.ip_addr = ip_addr
        self.port = port
        self.cpus = cpus
        self.name = None
        self.jobs = 0
        self.clusters = []
        self.cpu_time = 0
        self.busy = 0
        self.files_xferred = {}
        self.compute_files_xferred = {}
        self.auth_code = hashlib.sha1(_xor_string(sign, secret)).hexdigest()
        self.sign = sign
        self.secret = secret
        self.keyfile = keyfile
        self.certfile = certfile
        self.last_pulse = None
        logging.debug('Auth for %s: %s, %s, %s', ip_addr, self.auth_code,
                      self.keyfile, self.certfile)

    def setup(self, compute, coro=None):
        # generator
        assert coro is not None
        logging.debug('Sending computation "%s" to %s', compute.name, self.ip_addr)
        resp = yield self.send('COMPUTE:' + cPickle.dumps(compute), coro=coro)

        if isinstance(resp, str) and resp.startswith('ACK'):
            resp = resp[len('ACK'):]
            if resp.startswith(':XFER_FILES:'):
                resp = resp[len(':XFER_FILES:'):]
                try:
                    xfer_files = cPickle.loads(resp)
                except:
                    logging.error("Couldn't transfer file '%s'", xf.name)
                    yield -1; raise StopIteration
                for xf in xfer_files:
                    logging.debug('Sending "%s"', xf.name)
                    resp = yield self.xfer_file(xf, coro=coro)
                    if resp != 'ACK':
                        logging.error("Couldn't transfer file '%s'", xf.name)
                        yield -1; raise StopIteration
            elif not resp:
                pass
            else:
                logging.error('Invalid response to computation: %s', resp)
                yield -1; raise StopIteration
        else:
            logging.warning('Got "%s": Ignoring node %s', resp, self.ip_addr)
            yield -1; raise StopIteration
        yield 0

    def send(self, msg, reply=True, coro=None):
        # generator
        assert coro is not None
        logging.debug('Sending to %s:%s', self.ip_addr, self.port)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock = AsynCoroSocket(sock, blocking=False, keyfile=self.keyfile, certfile=self.certfile)
        sock.settimeout(5)
        try:
            yield sock.connect((self.ip_addr, self.port))
            yield sock.write(self.auth_code)
            yield sock.write_msg(msg)
            if reply:
                resp = yield sock.read_msg()
            else:
                resp = None
        except:
            logging.error("Couldn't connect to %s:%s, %s",
                          self.ip_addr, self.port, traceback.format_exc())
            raise
            # TODO: mark this node down, reschedule on different node?
            resp = None
        finally:
            sock.close()
        yield resp

    def xfer_file(self, xf, coro=None):
        # generator
        assert coro is not None
        logging.debug('XferFile: %s to %s', xf.name, self.ip_addr)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock = AsynCoroSocket(sock, blocking=False, keyfile=self.keyfile, certfile=self.certfile)
        sock.settimeout(5)
        try:
            yield sock.connect((self.ip_addr, self.port))
            msg = 'FILEXFER:' + cPickle.dumps(xf)
            yield sock.write(self.auth_code)
            yield sock.write_msg(msg)
            fd = open(xf.name, 'rb')
            while True:
                data = fd.read(10240000)
                if not data:
                    break
                yield sock.write(data)
            fd.close()
            resp = yield sock.read_msg()
            assert resp == 'ACK'
        except:
            logging.error("Couldn't transfer %s to %s", xf.name, self.ip_addr)
            raise
            # TODO: mark this node down, reschedule on different node?
            resp = None
        finally:
            sock.close()
        yield resp

    def close(self, compute, coro=None):
        # generator
        assert coro is not None
        logging.debug('Closing node %s for %s / %s / %s', self.ip_addr, compute.name, compute.id,
                      compute.cleanup)
        msg = 'DEL_COMPUTE:' + cPickle.dumps({'ID':compute.id})
        try:
            v = yield self.send(msg, reply=False, coro=coro)
        except:
            logging.debug('Deleting computation %s/%s from %s failed',
                          compute.id, compute.name, self.ip_addr)

class _DispyJob_(object):
    """Internal use only.
    """
    def __init__(self, compute_id, args, kwargs):
        self.job = DispyJob()
        self.job._dispy_job_ = self
        self.uid = None
        self.compute_id = compute_id
        self.hash = os.urandom(20).encode('hex')
        self.node = None
        self.files = []
        job_deps = kwargs.pop('dispy_job_depends', [])
        if not isinstance(job_deps, list):
            job_deps = list(job_deps)
        self.args = cPickle.dumps(args)
        self.kwargs = cPickle.dumps(kwargs)
        depend_ids = {}
        for dep in job_deps:
            if isinstance(dep, str) or inspect.ismodule(dep):
                if inspect.ismodule(dep):
                    dep = dep.__file__
                    if dep.endswith('.pyc'):
                        dep = dep[:-1]
                    if not dep.endswith('.py'):
                        logging.warning('Invalid module "%s" - must be python source.' % dep)
                        continue
                        #raise Exception('Invalid module "%s" - must be python source.' % dep)
                if dep in depend_ids:
                    continue
                try:
                    fd = open(dep, 'rb')
                    fd.close()
                except:
                    logging.warning('File "%s" is not valid' % dep)
                    continue
                    # raise Exception('File "%s" is not valid' % dep)
                sbuf = os.stat(dep)
                # TODO: Check/limit size
                fd = open(dep, 'rb')
                data = fd.read()
                fd.close()
                self.files.append({'name':dep, 'stat':sbuf, 'data':data})
                depend_ids[dep] = dep
            elif inspect.isfunction(dep) or inspect.ismethod(dep) or \
                     inspect.isclass(dep) or hasattr(dep, '__class__'):
                if inspect.isfunction(dep) or inspect.isclass(dep):
                    pass
                elif inspect.ismethod(dep):
                    dep = dep.im_class
                elif hasattr(dep, '__class__') and inspect.isclass(dep.__class__):
                    dep = dep.__class__
                if id(dep) in depend_ids:
                    continue
                lines = inspect.getsourcelines(dep)[0]
                lines[0] = lines[0].lstrip()
                self.code += '\n' + ''.join(lines)
                depend_ids[id(dep)] = id(dep)
            else:
                logging.warning('Invalid job depends element "%s"; ignoring it.', dep)

    def __getstate__(self):
        state = {'uid':self.uid, 'hash':self.hash, 'compute_id':self.compute_id,
                 'args':self.args, 'kwargs':self.kwargs, 'files':self.files}
        return state

    def run(self, coro=None):
        # generator
        assert coro is not None
        logging.debug('running job %s on %s', self.uid, self.node.ip_addr)
        self.job.start_time = time.time()
        resp = yield self.node.send('JOB:' + cPickle.dumps(self), coro=coro)
        # TODO: deal with NAKs (reschedule?)
        if resp != 'ACK':
            logging.warning('Failed to run %s on %s', self.uid, self.node.ip_addr)
            raise Exception(str(resp))
        self.job.status = DispyJob.Running
        yield resp

    def finish(self, status):
        job = self.job
        job.status = status
        if status != DispyJob.ProvisionalResult:
            self.job._dispy_job_ = None
            self.job = None
        job.finish.set()

class _JobReply(object):
    """Internal use only.
    """
    def __init__(self, _job, ip_addr, status=None, keyfile=None, certfile=None):
        self.uid = _job.uid
        self.hash = _job.hash
        self.ip_addr = ip_addr
        self.status = status
        self.result = None
        self.stdout = None
        self.stderr = None
        self.exception = None

class _Cluster(object):
    """Internal use only.
    """
    __metaclass__ = MetaSingleton

    def __init__(self, ip_addr=None, port=None, node_port=None, secret='',
                 keyfile=None, certfile=None, shared=False):
        if not hasattr(self, 'ip_addr'):
            self.asyncoro = AsynCoro()
            atexit.register(self.shutdown)
            if ip_addr:
                ip_addr = _node_name_ipaddr(ip_addr)[1]
            else:
                ip_addr = socket.gethostbyname(socket.gethostname())
            if port is None:
                port = 51347
            if not node_port:
                node_port = 51348

            self.ip_addr = ip_addr
            self.port = port
            self.node_port = node_port
            self._nodes = {}
            self._secret = secret
            self.keyfile = keyfile
            self.certfile = certfile
            self.shared = shared
            self.pulse_interval = None
            self.ping_interval = None

            self.fault_recover_lock = CoroLock()
            self._clusters = {}
            self.unsched_jobs = 0
            self.job_uid = 1
            self._sched_jobs = {}
            self._sched_cv = CoroCondition()
            self.terminate_scheduler = False
            self.auth_code = os.urandom(20).encode('hex')

            self.timer_coro = Coro(self.timer_task)

            # self.select_job_node = self.fast_node_schedule
            self.select_job_node = self.load_balance_schedule
            self._scheduler = Coro(self._schedule_jobs)
            self.start_time = time.time()
            self.cluster_id = 1

            job_result_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            job_result_sock.bind((self.ip_addr, 0))
            job_result_sock.listen(32)
            self.job_result_port = job_result_sock.getsockname()[1]
            self.job_result_sock = AsynCoroSocket(job_result_sock, blocking=False,
                                                  keyfile=self.keyfile, certfile=self.certfile)
            logging.debug('job result server at %s:%s', self.ip_addr, self.job_result_port)
            self.job_result_coro = Coro(self.job_result_server)

            self.worker_Q = Queue.PriorityQueue()
            self.worker_thread = threading.Thread(target=self.worker)
            self.worker_thread.daemon = True
            self.worker_thread.start()

            if self.shared:
                self.udp_coro = None
            else:
                udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                udp_sock.bind(('', self.port))
                self.port = udp_sock.getsockname()[1]
                udp_sock = AsynCoroSocket(udp_sock, blocking=False)
                logging.info('UDP server running at %s, %s', self.ip_addr, self.port)
                self.udp_coro = Coro(self.udp_server, udp_sock)

    def send_ping_cluster(self, cluster, coro=None):
        # generator
        ping_request = cPickle.dumps({'scheduler_ip_addr':self.ip_addr,
                                      'scheduler_port':self.port, 'version':_dispy_version})
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock = AsynCoroSocket(sock, blocking=False)
        sock.settimeout(1)
        for node_spec, node_info in cluster._compute.node_spec.iteritems():
            if node_spec.find('*') >= 0:
                port = node_info['port']
                if not port:
                    port = self.node_port
                bc_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                bc_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                bc_sock = AsynCoroSocket(bc_sock, blocking=False)
                bc_sock.settimeout(1)
                try:
                    yield bc_sock.sendto('PING:%s' % ping_request, ('<broadcast>', port))
                except:
                    pass
                bc_sock.close()
            else:
                ip_addr = node_info['ip_addr']
                if ip_addr in cluster._compute.nodes:
                    continue
                port = node_info['port']
                if not port:
                    port = self.node_port
                try:
                    yield sock.sendto('PING:%s' % ping_request, (ip_addr, port))
                except:
                    pass
        sock.close()

    def add_cluster(self, cluster, coro=None):
        # generator (due to _sched_cv)
        assert coro is not None
        compute = cluster._compute
        self._sched_cv.acquire()
        if self.shared:
            assert cluster._compute.id not in self._clusters
            self._clusters[compute.id] = cluster
            for xf in compute.xfer_files:
                xf.compute_id = compute.id
            self._sched_cv.release()
            raise StopIteration
        else:
            self._clusters[self.cluster_id] = cluster
            cluster._id = self.cluster_id
            compute.id = cluster._id
            for xf in compute.xfer_files:
                xf.compute_id = compute.id
            self.cluster_id += 1
        self._sched_cv.release()

        if compute.pulse_interval:
            self.pulse_interval = max(self.pulse_interval, compute.pulse_interval)
        if cluster.ping_interval:
            self.ping_interval = max(self.ping_interval, cluster.ping_interval)
        if self.pulse_interval or self.ping_interval:
            self.timer_coro.resume(True)

        yield self.send_ping_cluster(cluster, coro=coro)
        self._sched_cv.acquire()
        compute_nodes = []
        for node_spec, host in compute.node_spec.iteritems():
            for ip_addr, node in self._nodes.iteritems():
                if ip_addr in compute.nodes:
                    continue
                if re.match(node_spec, ip_addr):
                    compute_nodes.append(node)
                    break
        self._sched_cv.notify()
        yield self._sched_cv.release()

        for node in compute_nodes:
            r = yield node.setup(compute, coro=coro)
            if r:
                logging.warning('Failed to setup %s for compute "%s"',
                                node.ip_addr, compute)
            else:
                self._sched_cv.acquire()
                if node.ip_addr not in compute.nodes:
                    compute.nodes[node.ip_addr] = node
                    node.clusters.append(compute.id)
                    self._sched_cv.notify()
                yield self._sched_cv.release()

    def del_cluster(self, cluster, coro=None):
        # generator
        self._sched_cv.acquire()
        if self._clusters.pop(cluster._compute.id, None) != cluster:
            logging.warning('cluster %s already closed?', cluster._compute.name)
            self._sched_cv.release()
            raise StopIteration

        if cluster._jobs or cluster._pending_jobs:
            logging.warning('cluster %s has pending jobs', compute.name)
            self._sched_cv.release()
            raise StopIteration

        self._sched_cv.release()

        if self.shared:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock = AsynCoroSocket(sock, blocking=False,
                                  keyfile=cluster.keyfile, certfile=cluster.certfile)
            sock.settimeout(2)
            yield sock.connect((cluster.scheduler_ip_addr, cluster.scheduler_port))
            req = 'DEL_COMPUTE:' + cPickle.dumps({'ID':cluster._compute.id})
            yield sock.write(cluster.auth_code)
            yield sock.write_msg(req)
            sock.close()
        else:
            self._sched_cv.acquire()
            nodes = cluster._compute.nodes.values()
            for node in nodes:
                logging.debug('node %s is being removed', node.ip_addr)
                node.clusters.remove(cluster._compute.id)
            cluster._compute.nodes = {}
            yield self._sched_cv.release()
            for node in nodes:
                yield node.close(cluster._compute, coro=coro)

        if cluster.fault_recover_file:
            self.fault_recover_lock.acquire()
            shelf = shelve.open(cluster.fault_recover_file, flag='r')
            n = len(shelf)
            shelf.close()
            if n == 0:
                try:
                    os.remove(cluster.fault_recover_file)
                except:
                    pass
            self.fault_recover_lock.release()

    def worker(self):
        # used for user callbacks only
        while True:
            item = self.worker_Q.get(block=True)
            priority, func, args = item
            if func is None:
                assert args is None
                self.worker_Q.task_done()
                break
            try:
                func(*args)
            except:
                logging.debug('Running %s failed: %s', func.__name__, traceback.format_exc())
            self.worker_Q.task_done()

    def finish_job(self, _job, status, cluster):
        # non-generator called with _sched_cv locked
        job = _job.job
        if cluster.callback:
            self.worker_Q.put((20, self.run_job_callback, (status, _job, cluster)))
        else:
            _job.finish(status)
            if status != DispyJob.ProvisionalResult:
                if cluster.fault_recover_file:
                    self.fault_recover_lock.acquire()
                    shelf = shelve.open(cluster.fault_recover_file, flag='c')
                    try:
                        del shelf[str(_job.uid)]
                    except:
                        # logging.warning('Apparently job %s is recovered?', _job.uid)
                        pass
                    shelf.close()
                    self.fault_recover_lock.release()
                assert cluster._pending_jobs > 0
                cluster._pending_jobs -= 1
                if cluster._pending_jobs == 0:
                    cluster.end_time = time.time()
                    cluster._complete.set()

    def run_job_callback(self, status, _job, cluster):
        # called from worker
        job = _job.job
        _job.finish(status)
        try:
            cluster.callback(job)
        except:
            if job.exception:
                job.exception += traceback.format_exc()
            else:
                job.exception = traceback.format_exc()
        finally:
            def _finish_job(self, _job, cluster, coro=None):
                if cluster.fault_recover_file:
                    self.fault_recover_lock.acquire()
                    shelf = shelve.open(cluster.fault_recover_file, flag='c')
                    try:
                        del shelf[str(_job.uid)]
                    except:
                        # logging.warning('Apparently job %s is recovered?', _job.uid)
                        pass
                    shelf.close()
                    self.fault_recover_lock.release()
                self._sched_cv.acquire()
                assert cluster._pending_jobs > 0
                cluster._pending_jobs -= 1
                if cluster._pending_jobs == 0:
                    cluster.end_time = time.time()
                    cluster._complete.set()
                yield self._sched_cv.release()
                
            if status != DispyJob.ProvisionalResult:
                Coro(_finish_job, self, _job, cluster).value()

    def setup_node(self, node, computations, coro=None):
        # generator
        assert coro is not None
        for compute in computations:
            r = yield node.setup(compute, coro=coro)
            if r:
                logging.warning('Failed to setup %s for compute "%s"',
                                node.ip_addr, compute)
            else:
                self._sched_cv.acquire()
                if node.ip_addr not in compute.nodes:
                    compute.nodes[node.ip_addr] = node
                    node.clusters.append(compute.id)
                    self._sched_cv.notify()
                yield self._sched_cv.release()

    def run_job(self, _job, cluster, coro=None):
        # generator
        assert coro is not None
        if cluster.fault_recover_file:
            self.store_job_info(_job, cluster)
            # not really necessary to remove it in case of exception
        try:
            yield _job.run(coro=coro)
        except EnvironmentError:
            logging.warning('Failed to run job %s on %s for computation %s; removing this node',
                            _job.uid, _job.node.ip_addr, cluster._compute.name)
            self._sched_cv.acquire()
            if cluster._compute.nodes.pop(_job.node.ip_addr, None) is not None:
                _job.node.clusters.remove(cluster.compute.id)
                # TODO: remove the node from all clusters and globally?
            # this job might have been deleted already due to timeout
            if self._sched_jobs.pop(_job.uid, None) == _job:
                cluster._jobs.insert(0, _job)
                self.unsched_jobs += 1
                _job.node.busy -= 1
            self._sched_cv.notify()
            yield self._sched_cv.release()
        except:
            logging.warning('Failed to run job %s on %s for computation %s; rescheduling it',
                            _job.uid, _job.node.ip_addr, cluster._compute.name)
            logging.debug(traceback.format_exc())
            # TODO: delay executing again for some time?
            self._sched_cv.acquire()
            # this job might have been deleted already due to timeout
            if self._sched_jobs.pop(_job.uid, None) == _job:
                cluster._jobs.append(_job)
                self.unsched_jobs += 1
                _job.node.busy -= 1
            self._sched_cv.notify()
            yield self._sched_cv.release()

    def udp_server(self, udp_sock, coro=None):
        # generator
        assert coro is not None
        while True:
            msg, addr = yield udp_sock.recvfrom(1024)
            if msg.startswith('PULSE:'):
                msg = msg[len('PULSE:'):]
                try:
                    info = cPickle.loads(msg)
                    node = self._nodes[info['ip_addr']]
                    assert 0 <= info['cpus'] <= node.cpus
                    node.last_pulse = time.time()
                    msg = 'PULSE:' + cPickle.dumps({'ip_addr':self.ip_addr})
                    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    sock = AsynCoroSocket(sock, blocking=False)
                    sock.settimeout(1)
                    yield sock.sendto(msg, (info['ip_addr'], info['port']))
                    sock.close()
                except:
                    logging.warning('Ignoring pulse message from %s', addr[0])
                    #logging.debug(traceback.format_exc())
                    continue
            elif msg.startswith('PONG:'):
                try:
                    status = cPickle.loads(msg[len('PONG:'):])
                    assert status['port'] > 0 and status['cpus'] > 0
                    assert len(status['ip_addr']) > 0
                    assert status['version'] == _dispy_version
                except:
                    logging.debug('Ignoring node %s', addr[0])
                    continue
                logging.debug('Discovered %s:%s with %s cpus',
                              status['ip_addr'], status['port'], status['cpus'])
                if status['cpus'] <= 0:
                    logging.debug('Ignoring node %s', status['ip_addr'])
                    continue
                self._sched_cv.acquire()
                node = self._nodes.get(status['ip_addr'], None)
                if node is None:
                    node = _Node(status['ip_addr'], status['port'],
                                 status['cpus'], status['sign'],
                                 self._secret, keyfile=self.keyfile, certfile=self.certfile)
                    self._nodes[node.ip_addr] = node
                else:
                    node.last_pulse = time.time()
                    h = _xor_string(status['sign'], self._secret)
                    h = hashlib.sha1(h).hexdigest()
                    if node.port == status['port'] and node.auth_code == h:
                        yield self._sched_cv.release()
                        continue
                    node.port = status['port']
                    node.auth_code = h
                node_computations = []
                for cid, cluster in self._clusters.iteritems():
                    compute = cluster._compute
                    if node.ip_addr in compute.nodes:
                        continue
                    for node_spec, host in compute.node_spec.iteritems():
                        if re.match(node_spec, node.ip_addr):
                            if host['name'].find('*') >= 0:
                                node.name = node.ip_addr
                            else:
                                node.name = host['name']
                            node_computations.append(compute)
                            break
                yield self._sched_cv.release()
                if node_computations:
                    Coro(self.setup_node, node, node_computations)
            elif msg.startswith('TERMINATED:'):
                try:
                    data = cPickle.loads(msg[len('TERMINATED:'):])
                    self._sched_cv.acquire()
                    node = self._nodes.pop(data['ip_addr'], None)
                    if not node:
                        yield self._sched_cv.release()
                        continue
                    logging.debug('Removing node %s', node.ip_addr)
                    h = _xor_string(data['sign'], self._secret)
                    auth_code = hashlib.sha1(h).hexdigest()
                    if auth_code != node.auth_code:
                        logging.warning('Invalid signature from %s', node.ip_addr)
                    dead_jobs = [_job for _job in self._sched_jobs.itervalues() \
                                 if _job.node is not None and \
                                 _job.node.ip_addr == node.ip_addr]
                    self.reschedule_jobs(dead_jobs, coro)
                    for cid, cluster in self._clusters.iteritems():
                        if cluster._compute.nodes.pop(node.ip_addr, None) is not None:
                            node.clusters.remove(cid)
                    yield self._sched_cv.release()
                    del node
                except:
                    logging.debug('Removing node failed: %s', traceback.format_exc())
                    pass
            else:
                logging.debug('Ignoring UDP message %s from: %s',
                              msg[:min(5, len(msg))], addr[0])

    def job_result_server(self, coro=None):
        # generator
        assert coro is not None
        while True:
            try:
                new_sock, addr = yield self.job_result_sock.accept()
            except ssl.SSLError, err:
                logging.debug('SSL connection failed: %s', str(err))
                continue
            except GeneratorExit:
                break
            except:
                logging.debug(traceback.format_exc())
                continue
            # logging.debug('received job result from %s', str(addr))
            new_sock.settimeout(2)
            Coro(self.job_result_task, new_sock, addr)

    def job_result_task(self, sock, addr, coro=None):
        # generator
        assert coro is not None
        try:
            msg = yield sock.read_msg()
            yield sock.write_msg('ACK')
            reply = cPickle.loads(msg)
        except:
            logging.warning('Failed to read job result from %s: %s',
                            str(addr), traceback.format_exc())
            raise StopIteration
        finally:
            sock.close()

        self._sched_cv.acquire()
        node = self._nodes.get(addr[0], None)
        _job = self._sched_jobs.get(reply.uid, None)
        if (node is None and self.shared is False) or (_job is None):
            self._sched_cv.release()
            logging.warning('Ignoring invalid reply for job %s from %s',
                            reply.uid, addr[0])
            raise StopIteration
        logging.debug('Received reply for job %s from %s', _job.uid, addr[0])
        cluster = self._clusters.get(_job.compute_id, None)
        if cluster is None:
            # job cancelled while closing computation?
            if self.shared is False:
                assert node.busy > 0
                node.busy -= 1
            self._sched_cv.release()
            raise StopIteration
        compute = cluster._compute
        job = _job.job
        try:
            assert reply.hash == _job.hash
            job.result = reply.result
            job.stdout = reply.stdout
            job.stderr = reply.stderr
            job.exception = reply.exception
            if self.shared:
                job.ip_addr = reply.ip_addr
                job.start_time = getattr(reply, 'start_time', None)
                job.end_time = getattr(reply, 'end_time', None)
            else:
                job.end_time = time.time()
                assert reply.ip_addr == node.ip_addr
                job.ip_addr = node.ip_addr
                node.last_pulse = time.time()
            if reply.status == DispyJob.ProvisionalResult:
                self.finish_job(_job, DispyJob.ProvisionalResult, cluster)
                self._sched_cv.release()
                raise StopIteration
            else:
                del self._sched_jobs[_job.uid]
        except:
            self._sched_cv.release()
            logging.warning('Invalid job result for %s from %s',
                            _job.uid, addr[0])
            logging.debug('%s, %s', str(reply), traceback.format_exc())
            raise StopIteration

        if self.shared is False:
            if reply.status == DispyJob.Terminated:
                assert job.status in [DispyJob.Running, DispyJob.Cancelled,
                                      DispyJob.Terminated], \
                       'invalid job status for %s: %s' % (_job.uid, job.status)
            else:
                assert job.status in [DispyJob.Running, DispyJob.ProvisionalResult], \
                       'status of %s: %s, %s' % (_job.uid, job.status, reply.status)
                node.jobs += 1
            node.busy -= 1
            node.cpu_time += job.end_time - job.start_time
        else:
            if addr[0] != cluster.scheduler_ip_addr:
                logging.warning('Ignoring job reply from %s', addr[0])
                self._sched_cv.release()
                raise StopIteration
            node = self._nodes.get(job.ip_addr, None)
            if node is None:
                node = _Node(job.ip_addr, 0, getattr(reply, 'cpus', 0), '', None,
                             keyfile=None, certfile=None)
                self._nodes[job.ip_addr] = node
                compute.nodes[job.ip_addr] = node
            job.status = reply.status
            if job.status == DispyJob.Finished:
                node.jobs += 1
            if job.end_time is not None:
                node.cpu_time += job.end_time - job.start_time
        self.finish_job(_job, reply.status, cluster)
        self._sched_cv.notify()
        self._sched_cv.release()
        raise StopIteration # job_result_task

    def reschedule_jobs(self, dead_jobs, coro):
        # non-generator, called with _sched_cv locked
        for _job in dead_jobs:
            # TODO: should we send terminate request to the node?
            cluster = self._clusters[_job.compute_id]
            del self._sched_jobs[_job.uid]
            if cluster._compute.reentrant:
                logging.debug('Rescheduling job %s from %s',
                              _job.uid, _job.node.ip_addr)
                _job.job.status = DispyJob.Created
                cluster._jobs.append(_job)
                self.unsched_jobs += 1
            else:
                logging.debug('Terminating job %s scheduled on %s',
                              _job.uid, _job.node.ip_addr)
                if _job.job.status == DispyJob.Running:
                    status = DispyJob.Terminated
                else:
                    status = DispyJob.Cancelled
                self.finish_job(_job, status, cluster)

    def timer_task(self, coro=None):
        reset = True
        last_pulse_time = last_ping_time = time.time()
        while True:
            if reset:
                if self.pulse_interval:
                    self.pulse_timeout = 5.0 * self.pulse_interval
                else:
                    self.pulse_timeout = None
                if self.pulse_timeout and self.ping_interval:
                    timeout = min(self.pulse_timeout, self.ping_interval)
                else:
                    timeout = max(self.pulse_timeout, self.ping_interval)

            reset = yield coro.suspend(timeout)
            now = time.time()
            if self.pulse_timeout and (now - last_pulse_time) >= self.pulse_timeout:
                last_pulse_time = now
                if self.shared:
                    self._sched_cv.acquire()
                    clusters = self._clusters.values()
                    yield self._sched_cv.release()
                    for cluster in clusters:
                        msg = {'client_scheduler_ip_addr':self.ip_addr,
                               'client_scheduler_port':self.port,
                               'version':_dispy_version}
                        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                        sock = AsynCoroSocket(sock, blocking=False)
                        sock.settimeout(1)
                        yield sock.sendto('PULSE:' + cPickle.dumps(msg),
                                          (cluster.scheduler_ip_addr, cluster.scheduler_udp_port))
                        sock.close()
                else:
                    self._sched_cv.acquire()
                    dead_nodes = {}
                    for node in self._nodes.itervalues():
                        if node.busy and node.last_pulse is not None and \
                               (node.last_pulse + self.pulse_timeout) <= now:
                            logging.warning('Node %s is not responding; removing it (%s, %s, %s)',
                                            node.ip_addr, node.busy, node.last_pulse, now)
                            dead_nodes[node.ip_addr] = node
                    for ip_addr in dead_nodes:
                        del self._nodes[ip_addr]
                        for cluster in self._clusters.itervalues():
                            cluster._compute.nodes.pop(ip_addr, None)
                    dead_jobs = [_job for _job in self._sched_jobs.itervalues() \
                                 if _job.node is not None and _job.node.ip_addr in dead_nodes]
                    if dead_nodes or dead_jobs:
                        self._sched_cv.notify()
                    self.reschedule_jobs(dead_jobs, coro)
                    yield self._sched_cv.release()

            if self.ping_interval and (now - last_ping_time) >= self.ping_interval:
                last_ping_time = now
                self._sched_cv.acquire()
                for cluster in self._clusters.itervalues():
                    Coro(self.send_ping_cluster, cluster)
                yield self._sched_cv.release()

    def load_balance_schedule(self):
        host = None
        load = None
        for node in self._nodes.itervalues():
            if node.busy >= node.cpus:
                continue
            if all(not self._clusters[cluster_id]._jobs for cluster_id in node.clusters):
                continue
            # logging.debug('load: %s, %s, %s' % (node.ip_addr, node.busy, node.cpus))
            if (load is None) or ((float(node.busy) / node.cpus) < load):
                load = float(node.busy) / node.cpus
                host = node
        return host

    def fast_node_schedule(self):
        # as we eagerly schedule, this has limited advantages
        # (useful only when  we have data about all the nodes and more than one node
        # is currently available)
        # in addition, we assume all jobs take equal time to execute
        host = None
        secs_per_job = None
        for node in self._nodes.itervalues():
            if node.busy >= node.cpus:
                continue
            if all(not self._clusters[cluster_id]._jobs for cluster_id in node.clusters):
                continue
            # logging.debug('load: %s, %s, %s' % (node.ip_addr, node.jobs, node.cpu_time))
            if (secs_per_job is None) or (node.jobs == 0) or \
                   (secs_per_job > (node.cpu_time / node.jobs)):
                if node.jobs == 0:
                    secs_per_job = 0
                else:
                    secs_per_job = node.cpu_time / node.jobs
                host = node
        return host

    def _schedule_jobs(self, coro=None):
        # generator
        assert coro is not None
        while True:
            self._sched_cv.acquire()
            # n = sum(len(cluster._jobs) for cluster in self._clusters.itervalues())
            # assert self.unsched_jobs == n, '%s != %s' % (self.unsched_jobs, n)
            while self._sched_cv.wait():
                yield None
            if self.terminate_scheduler:
                yield self._sched_cv.release()
                break
            while self.unsched_jobs:
                logging.debug('Pending jobs: %s', self.unsched_jobs)
                node = self.select_job_node()
                if node is None:
                    logging.debug('No nodes/jobs')
                    break
                # TODO: strategy to pick a cluster?
                for cid in node.clusters:
                    if self._clusters[cid]._jobs:
                        _job = self._clusters[cid]._jobs.pop(0)
                        break
                else:
                    break
                cluster = self._clusters[_job.compute_id]
                _job.node = node
                logging.debug('Scheduling job %s on %s (load: %.3f, %s)',
                              _job.uid, node.ip_addr, float(node.busy) / node.cpus, node.busy)
                assert node.busy < node.cpus
                self._sched_jobs[_job.uid] = _job
                self.unsched_jobs -= 1
                node.busy += 1
                Coro(self.run_job, _job, cluster)
            yield self._sched_cv.release()
        self._sched_cv.acquire()
        logging.debug('scheduler quitting (%s / %s)',
                      len(self._sched_jobs), self.unsched_jobs)
        for cid, cluster in self._clusters.iteritems():
            if not hasattr(cluster, '_compute'):
                # cluster is closed
                continue
            compute = cluster._compute
            for node in compute.nodes.itervalues():
                Coro(node.close, compute)
            for _job in cluster._jobs:
                logging.debug('Finishing job %s', _job.uid)
                # TODO: handle shared scheduler jobs appropriately
                if _job.job.status == DispyJob.Running:
                    # TODO: send terminate request to nodes?
                    status = DispyJob.Terminated
                else:
                    status = DispyJob.Cancelled
                self.finish_job(_job, status, cluster)
            cluster._jobs = []

        self._nodes = {}
        self.unsched_jobs = 0
        self._sched_jobs = {}
        yield self._sched_cv.release()
        logging.debug('scheduler quit')

    def store_job_info(self, _job, cluster):
        # non-generator
        if not cluster.fault_recover_file:
            return
        self.fault_recover_lock.acquire()
        shelf = shelve.open(cluster.fault_recover_file, flag='c')
        state = {'id':_job.job.id, 'hash':_job.hash, 'compute_id':_job.compute_id,
                 'args':_job.args, 'kwargs':_job.kwargs,
                 'ip_addr':_job.node.ip_addr, 'port':_job.node.port}
        shelf[str(_job.uid)] = shelve_repr(state)
        shelf.close()
        self.fault_recover_lock.release()
        return

    def submit_job(self, _job, coro=None):
        # generator
        assert coro is not None
        self._sched_cv.acquire()
        #_job.uid = id(_job)
        _job.uid = self.job_uid
        self.job_uid += 1
        if self.job_uid == sys.maxint:
            # TODO: check if it is okay to reset
            self.job_uid = 1
        cluster = self._clusters[_job.compute_id]
        cluster._jobs.append(_job)
        self.unsched_jobs += 1
        cluster._pending_jobs += 1
        cluster._complete.clear()
        logging.debug('adding job %s', _job.uid)
        self._sched_cv.notify()
        self._sched_cv.release()
        yield True

    def cancel_job(self, job, coro=None):
        # generator
        assert coro is not None
        assert self.shared is False
        self._sched_cv.acquire()
        _job = job._dispy_job_
        if _job is None:
            self._sched_cv.release()
            logging.warning('Job %s is invalid for cancellation!', job.id)
            yield -1; raise StopIteration
        cluster = self._clusters.get(_job.compute_id, None)
        if not cluster:
            logging.warning('Invalid job %s for cluster "%s"!',
                            _job.uid, cluster._compute.name)
            self._sched_cv.release()
            yield -1; raise StopIteration
        assert cluster._pending_jobs >= 1
        if _job.job.status == DispyJob.Created:
            assert _job.uid not in self._sched_jobs
            cluster._jobs.remove(_job)
            self.unsched_jobs -= 1
            self.finish_job(_job, DispyJob.Cancelled, cluster)
            self._sched_cv.release()
            logging.debug('Cancelled (removed) job %s', _job.uid)
            yield 0; raise StopIteration
        elif not (_job.job.status == DispyJob.Running or \
                  _job.job.status == DispyJob.ProvisionalResult):
            self._sched_cv.release()
            logging.warning('Job %s is not valid for cancel (%s)', _job.uid, _job.job.status)
            yield -1; raise StopIteration
        _job.job.status = DispyJob.Cancelled
        yield self._sched_cv.release()
        if _job.node is not None:
            try:
                yield _job.node.send('TERMINATE_JOB:' + cPickle.dumps(_job), reply=False, coro=coro)
                logging.debug('Job %s is being terminated', _job.uid)
            except:
                logging.warning('Terminating job %s failed: %s', _job.uid, traceback.format_exc())
                yield -1; raise StopIteration
        yield 0

    def shutdown(self):
        # non-generator
        def _shutdown(self, coro=None):
            # generator
            assert coro is not None
            # TODO: make sure JobCluster instances are done
            if not hasattr(self, '_scheduler'):
                raise StopIteration
            self._sched_cv.acquire()
            select_job_node = self.select_job_node
            self.select_job_node = None
            if select_job_node:
                logging.debug('shutting down scheduler ...')
                clusters = self._clusters.values()
                self._sched_cv.release()
                for cluster in clusters:
                    yield self.del_cluster(cluster, coro=coro)

                self._sched_cv.acquire()
                for uid, _job in self._sched_jobs.iteritems():
                    # TODO: handle shared scheduler jobs appropriately
                    if _job.job.status == DispyJob.Running:
                        # TODO: send terminate request to nodes?
                        status = DispyJob.Terminated
                    else:
                        status = DispyJob.Cancelled
                    self.finish_job(_job, status, self._clusters[_job.compute_id])

                self._clusters = {}
                self._sched_jobs = {}
                self.terminate_scheduler = True
                self._sched_cv.notify()
                yield self._sched_cv.release()
                self.worker_Q.put((99, None, None))
            else:
                self._sched_cv.release()
            yield None
        Coro(_shutdown, self).value()
        self._scheduler.value()
        self.worker_Q.join()
        self.timer_coro.terminate()
        self.job_result_coro.terminate()
        if self.udp_coro:
            self.udp_coro.terminate()
        self.asyncoro.join()
        self.asyncoro.terminate()
        logging.debug('shutdown complete')

    def stats(self, compute, wall_time=None):
        print
        heading = '%020s  |  %05s  |  %05s  |  %010s  |  %13s' % \
                  ('Node', 'CPUs', 'Jobs', 'Sec/Job', 'Node Time Sec')
        print heading
        print '-' * len(heading)
        cpu_time = 0.0
        for ip_addr in sorted(compute.nodes, key=lambda addr: compute.nodes[addr].jobs,
                              reverse=True):
            node = compute.nodes[ip_addr]
            if node.jobs:
                secs_per_job = node.cpu_time / node.jobs
            else:
                secs_per_job = 0
            cpu_time += node.cpu_time
            print '%020s  |  %05s  |  %05s  |  %10.3f  |  %13.3f' % \
                  (ip_addr, node.cpus, node.jobs, secs_per_job, node.cpu_time)
        print
        msg = 'Total job time: %.3f sec' % cpu_time
        if wall_time:
            msg += ', wall time: %.3f sec, speedup: %.3f' % (wall_time, cpu_time / wall_time)
        print msg
        print

class JobCluster(object):
    """Create an instance of cluster for a specific job.
    """

    def __init__(self, computation, nodes=['*'], depends=[], callback=None, max_cpus=None,
                 ip_addr=None, port=None, node_port=None, dest_path=None,
                 loglevel=logging.WARNING, cleanup=True, ping_interval=None, pulse_interval=None,
                 reentrant=False, secret='', keyfile=None, certfile=None, fault_recover=None):
        """Create an instance of cluster for a specific computation.

        @computation is either a string (which is name of program, possibly
        with full path) or a python function or class method.

        @nodes is a list. Each element of @nodes is either a string
          (which must be either IP address or name of server node), or
          a tuple with up to 3 elements.  The tuple's first element
          must be IP address or name of server node, second element,
          if present, must be port number where that node is listening
          for ping from clients, the third element, if present, must
          be number of CPUs to use on that node.

        @depends is a list. Each element of @depends is either
          a string or a python object. If the element is a string,
          it must be a file which will be transferred to the node
          executing a job for this cluster.
          If the element is a python object (a function name, class name etc.),
          then the code for that object is transferred to the node executing
          a job for this cluster.

       @callback is a function or class method. When a job's results
          become available, dispy will call provided callback
          function/method with that job as the argument. If a job
          sends provisional results with 'dispy_provisional_result'
          multiple times, then dispy will call provided callback each
          such time. The (provisional) results of computation can be
          retrieved with 'result' field of job, etc. While
          computations are run on nodes in isolated environments,
          callbacks are run in the context of user programs from which
          (Shared)JobCluster is called - for example, callbacks can
          access global variables in user programs.

        @ip_addr and @port indicate the address where the cluster will bind to.
          If multiple instances of JobCluster are used, these arguments are used
          only in the case of first instance.
          If no value for @ip_addr is given (default), IP address associated
          with the 'hostname' is used.
          If no value for @port is given (default), number 51347 is used.

        @node_port indicates port on which node servers are listening
          for ping messages. The client (JobCluster instance) broadcasts
          ping requests to this port.
          If no value for @node_port is given (default), number 51348 is used.

        @dest_path indicates path of directory to which files are
          transferred to a server node when executing a job.  If
          @computation is a string, indicating a program, then that
          program is also transferred to @dest_path.
        
        @loglevel indicates message priority for logging module.

        @cleanup indicates if the files transferred should be removed when
          shutting down.

        @secret is a string that is (hashed and) used for handshaking
          of communication with nodes.

        @certfile is path to file containing SSL certificate (see
          Python 'ssl' module).

        @keyfile is path to file containing private key for SSL
          communication (see Python 'ssl' module). This key may be
          stored in 'certfile' itself, in which case this should be
          None.

        @ping_interval is number of seconds between 1 and
        1000. Normally dispy can find nodes running 'dispynode' by
        broadcasting 'ping' messages that nodes respond to. However,
        these packets may get lost. If ping_interval is set, then
        every ping_interval seconds, dispy sends ping messages to find
        nodes that may have missed earlier ping messages.

        @pulse_interval is number of seconds between 1 and 1000. If
        pulse_interval is set, dispy directs nodes to send 'pulse'
        messages to indicate they are computing submitted jobs. A node
        is presumed dead if 5*pulse_interval elapses without a pulse
        message. See 'reentrant' below.

        @reentrant must be either True or False. This value is used
        only if 'pulse_interval' is set for any of the clusters. If
        pulse_interval is given and reentrant is False (default), jobs
        scheduled for a dead node are automatically cancelled; if
        reentrant is True, then jobs scheduled for a dead node are
        resubmitted to other eligible nodes.

        @fault_recover must be either True or file path. When this
        is True, dispy stores information about jobs in a file of the
        form '_dispy_fault_recover_YYYYMMDDHHMMSS' in current directory. If it
        is a path, dispy will use given path to store information
        about jobs. If user program terminates for some reason (such
        as raising an exception), it is possible to retrieve results
        of scheduled jobs later (after they are finished) by calling
        'fault_recover_jobs' function (implemented in this file) with this
        file.

        """

        logging.basicConfig(format='%(asctime)s %(message)s', level=loglevel)

        if not isinstance(nodes, list):
            if isinstance(nodes, str):
                nodes = [nodes]
            else:
                raise Exception('"nodes" must be list of IP addresses or host names')
        if reentrant != True and reentrant != False:
            logging.warning('Invalid value for reentrant (%s) is ignored; ' \
                            'it must be either True or False' % reentrant)
            reentrant = False
        if ping_interval is not None:
            try:
                ping_interval = float(ping_interval)
                assert 1.0 <= ping_interval <= 1000
            except:
                raise Exception('Invalid ping_interval; must be between 1 and 1000')
        self.ping_interval = ping_interval
        if pulse_interval is not None:
            try:
                pulse_interval = float(pulse_interval)
                assert 1.0 <= pulse_interval <= 1000
            except:
                raise Exception('Invalid pulse_interval; must be between 1 and 1000')

        if callback:
            assert inspect.isfunction(callback) or inspect.ismethod(callback), \
                   "callback must be a function or method"
            try:
                args = inspect.getargspec(callback)
                if inspect.isfunction(callback):
                    assert len(args.args) == 1
                else:
                    assert len(args.args) == 2
                    if args.args[0] != 'self':
                        logging.warning('First argument to callback method is not "self"')
                assert args.varargs is None
                assert args.keywords is None
                assert args.defaults is None
            except:
                raise Exception("Invalid callback function; "
                                "it must take excatly one argument - an instance of DispyJob")
        self.callback = callback
        if hasattr(self, 'scheduler_udp_port'):
            shared = True
        else:
            shared = False

        if fault_recover:
            if fault_recover is True:
                now = datetime.datetime.now()
                self.fault_recover_file = '_dispy_fault_recover_%.4i%.2i%.2i%.2i%.2i%.2i' % \
                                          (now.year, now.month, now.day,
                                           now.hour, now.minute, now.second)
            elif isinstance(fault_recover, str):
                self.fault_recover_file = fault_recover
            else:
                raise Exception('Invalid fault_recover option: "%s"' % fault_recover)
        else:
            self.fault_recover_file = None

        if self.fault_recover_file:
            try:
                shelf = shelve.open(self.fault_recover_file, flag='c')
                shelf.close()
            except:
                raise Exception('Could not create fault recover file "%s"' % \
                                self.fault_recover_file)

            # TODO?: it is safer to use file locking instead of thread
            # locking, but it is more efficient to use thread locking
            # (in this case). However, 'fault_recover_jobs' function
            # must not be used when a cluster using the same file is
            # also active
            logging.info('Storing fault recovery information in "%s"', self.fault_recover_file)

        self._cluster = _Cluster(ip_addr=ip_addr, port=port, node_port=node_port,
                                 secret=secret, keyfile=keyfile, certfile=certfile,
                                 shared=shared)

        self.ip_addr = self._cluster.ip_addr

        if inspect.isfunction(computation) or inspect.ismethod(computation):
            func = computation
            compute = _Compute(_Compute.func_type, func.func_name)
            # compute.env = {'PYTHONPATH':[os.getcwd()] + sys.path}
            lines = inspect.getsourcelines(func)[0]
            lines[0] = lines[0].lstrip()
            compute.code = ''.join(lines)
        elif isinstance(computation, str):
            compute = _Compute(_Compute.prog_type, computation)
            compute.env = {'PATH':os.getenv('PATH')}
            depends.append(computation)
        else:
            raise Exception('Invalid computation type: %s' % type(compute))
        depend_ids = {}
        for dep in depends:
            if isinstance(dep, str) or inspect.ismodule(dep):
                if inspect.ismodule(dep):
                    dep = dep.__file__
                    if dep.endswith('.pyc'):
                        dep = dep[:-1]
                    if not (dep.endswith('.py') and os.path.isfile(dep)):
                        raise Exception('Invalid module "%s" - must be python source.' % dep)
                if dep in depend_ids:
                    continue
                if compute.type == _Compute.prog_type and not os.path.isfile(dep):
                    for p in compute.env['PATH'].split(':'):
                        f = os.path.join(p, dep)
                        if os.path.isfile(f):
                            logging.debug('Assuming "%s" is program "%s"', dep, f)
                            dep = f
                            break
                    else:
                        raise Exception('Program "%s" is not valid' % dep)
                try:
                    fd = open(dep, 'rb')
                    fd.close()
                    xf = _XferFile(dep, os.stat(dep), compute.id)
                    compute.xfer_files.append(xf)
                    depend_ids[dep] = dep
                except:
                    raise Exception('File "%s" is not valid' % dep)
            elif inspect.isfunction(dep) or inspect.ismethod(dep) or \
                     inspect.isclass(dep) or hasattr(dep, '__class__'):
                if inspect.isfunction(dep) or inspect.isclass(dep):
                    pass
                elif inspect.ismethod(dep):
                    dep = dep.im_class
                elif hasattr(dep, '__class__') and inspect.isclass(dep.__class__):
                    dep = dep.__class__
                if id(dep) in depend_ids:
                    continue
                if compute.type == _Compute.prog_type:
                    raise Exception('Program computations cannot depend on "%s"' % dep.__name__)
                lines = inspect.getsourcelines(dep)[0]
                lines[0] = lines[0].lstrip()
                compute.code += '\n' + ''.join(lines)
                depend_ids[id(dep)] = id(dep)
            else:
                raise Exception('Invalid function/method: %s' % dep)
        if compute.code:
            # make sure code can be compiled
            code = compile(compute.code, '<string>', 'exec')
            del code
            compute.code = base64.b64encode(compute.code)
        if dest_path:
            if not isinstance(dest_path, str):
                raise Exception('Invalid dest_path: it must be a string')
            dest_path = dest_path.strip()
            # we should check for absolute path in dispynode.py as well
            if dest_path.startswith(os.sep):
                logging.warning('dest_path must not be absolute path')
            dest_path = dest_path.lstrip(os.sep)
            compute.dest_path = dest_path
            compute.cleanup = (cleanup == True)
        else:
            if not cleanup:
                logging.warning('"cleanup" argument is ignored if dest_path is not given')
            compute.cleanup = True

        compute.job_result_port = self._cluster.job_result_port
        compute.scheduler_ip_addr = self._cluster.ip_addr
        compute.scheduler_port = self._cluster.port
        compute.cleanup = cleanup
        compute.reentrant = reentrant
        compute.pulse_interval = pulse_interval

        if not shared:
            compute.node_spec = _parse_nodes(nodes)
            if not compute.node_spec:
                raise Exception('"nodes" argument is invalid')

        self._compute = compute
        if max_cpus is not None:
            max_cpus = int(max_cpus)
        self._max_cpus = max_cpus
        self._cpus = 0
        self._pending_jobs = 0
        self._jobs = []
        self._complete = threading.Event()
        self._complete.set()
        self.cpu_time = 0
        self.start_time = time.time()
        self.end_time = None
        if not shared:
            Coro(self._cluster.add_cluster, self).value()

    def submit(self, *args, **kwargs):
        """Submit a job for execution with the given arguments.

        Arguments should be serializable and should correspond to
        arguments for computation used when cluster is created.
        """
        if self._compute.type == _Compute.prog_type:
            if kwargs:
                logging.warning("Programs can't have keyword arguments")
                return None
            args = [str(arg) for arg in args]
        try:
            _job = _DispyJob_(self._compute.id, args, kwargs)
        except:
            logging.warning('Creating job for "%s", "%s" failed with "%s"',
                            str(args), str(kwargs), traceback.format_exc())
            return None
        v = Coro(self._cluster.submit_job, _job).value()
        assert v is True
        return _job.job

    def cancel(self, job):
        return Coro(self._cluster.cancel_job, job).value()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, trace):
        self.close()

    def stats(self):
        """Show statistics for cluster(s).

        Prints various statistics, such as number of nodes, CPUs in each node,
        jobs performed by each node and time taken for each job and all jobs.
        """
        if self.start_time is None or self.end_time is None:
            time = None
        else:
            time = self.end_time - self.start_time

        self._cluster.stats(self._compute, time)

    def wait(self):
        """Wait for scheduled jobs to complete.
        """
        self._complete.wait()

    def __call__(self):
        """Wait for scheduled jobs to complete.
        """
        self.wait()

    def close(self):
        logging.debug('Closing computation "%s"', self._compute.name)
        if hasattr(self, '_compute'):
            self._complete.wait()
            Coro(self._cluster.del_cluster, self).value()
            logging.debug('Computation "%s" deleted', self._compute.name)
            del self._compute

class SharedJobCluster(JobCluster):
    """SharedJobCluster should be used (instead of JobCluster) if two
    or more processes can simultaneously use dispy. In this case,
    'dispyscheduler' must be running on a node and 'scheduler_node'
    parameter should be set to that node's IP address or host name.

    @scheduler_node is name or IP address where dispyscheduler is
      running to which jobs are submitted.

    @port is port where dispyscheduler is running at @scheduler_node.

    @pulse_interval for SharedJobCluster is not used; instead,
    dispyscheduler must be called with appropriate pulse_interval.
    The behaviour is same as for JobCluster.

    SharedJobCluster does not support fault recovery (yet).
    """
    def __init__(self, computation, nodes=['*'], depends=[], callback=None,
                 ip_addr=None, port=51347, scheduler_node=None,
                 loglevel=logging.WARNING, cleanup=True,
                 pulse_interval=None, ping_interval=None, reentrant=False,
                 secret='', keyfile=None, certfile=None, fault_recover=None):
        if not isinstance(nodes, list):
            if isinstance(nodes, str):
                nodes = [nodes]
            else:
                raise Exception('"nodes" must be list of IP addresses or host names')
        self.scheduler_udp_port = port

        JobCluster.__init__(self, computation, nodes='dummy', depends=depends,
                            callback=callback, ip_addr=ip_addr, port=0,
                            cleanup=cleanup, pulse_interval=None,
                            reentrant=reentrant, loglevel=loglevel, fault_recover=fault_recover)
        if pulse_interval is not None:
            logging.warning('pulse_interval is not used in SharedJobCluster; ' \
                            'dispyscheduler should be started appropriately.')
        self.pulse_interval = None
        self.keyfile = keyfile
        self.certfile = certfile
        self._cluster.job_uid = None
        self._cluster.terminate_scheduler = True
        self._cluster._sched_cv.notify()
        self._compute.node_spec = nodes
        if scheduler_node:
            self.scheduler_ip_addr = _node_name_ipaddr(scheduler_node)[1]
        else:
            self.scheduler_ip_addr = self.ip_addr

        srv_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        srv_sock.settimeout(2)
        srv_sock.bind((self.ip_addr, 0))
        port_req = cPickle.dumps({'ip_addr':self.ip_addr, 'port':srv_sock.getsockname()[1]})
        for x in xrange(5):
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.sendto('SERVERPORT:' + port_req, (self.scheduler_ip_addr,
                                                       self.scheduler_udp_port))
                reply, addr = srv_sock.recvfrom(1024)
                reply = cPickle.loads(reply)
                logging.debug('Scheduler is %s:%s' % (reply['ip_addr'], reply['port']))
                if reply['version'] != _dispy_version:
                    raise Exception('dispyscheduler version "%s" is different from dispy version "%s"' % \
                                    reply['version'], _dispy_version)
                self.scheduler_port = reply['port']
                self.auth_code = hashlib.sha1(_xor_string(reply['sign'], secret)).hexdigest()
                logging.debug('auth_code: %s', self.auth_code)
                break
            except:
                continue
            finally:
                sock.close()
        else:
            raise Exception('Could not connect to scheduler at %s:%s',
                            self.scheduler_ip_addr, self.scheduler_udp_port)
        srv_sock.close()
        def _add_compute(self, coro=None):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock = AsynCoroSocket(sock, blocking=False,
                                  keyfile=self.keyfile, certfile=self.certfile)
            sock.settimeout(3)
            try:
                yield sock.connect((self.scheduler_ip_addr, self.scheduler_port))
                req = 'COMPUTE:' + cPickle.dumps(self._compute)
                yield sock.write(self.auth_code)
                yield sock.write_msg(req)
                msg = yield sock.read_msg()
                sock.close()
                resp = cPickle.loads(msg)
                self._compute.id = resp['ID']
                assert self._compute.id is not None
            except:
                logging.debug(traceback.format_exc())
                raise Exception("Couldn't connect to scheduler at %s:%s" % \
                                (self.scheduler_ip_addr, self.scheduler_port))
            if resp['pulse_interval'] and self._cluster.pulse_interval:
                self._cluster.pulse_interval = min(self._cluster.pulse_interval, resp['pulse_interval'])
            else:
                self._cluster.pulse_interval = max(self._cluster.pulse_interval, resp['pulse_interval'])

            for xf in self._compute.xfer_files:
                xf.compute_id = self._compute.id
                logging.debug('Sending file "%s"', xf.name)
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock = AsynCoroSocket(sock, blocking=False,
                                      keyfile=self.keyfile, certfile=self.certfile)
                sock.settimeout(5)
                try:
                    yield sock.connect((self.scheduler_ip_addr, self.scheduler_port))
                    msg = 'FILEXFER:' + cPickle.dumps(xf)
                    yield sock.write(self.auth_code)
                    yield sock.write_msg(msg)
                    fd = open(xf.name, 'rb')
                    while True:
                        data = fd.read(10240000)
                        if not data:
                            break
                        yield sock.write(data)
                    fd.close()
                    resp = yield sock.read_msg()
                    assert resp == 'ACK'
                except:
                    logging.error("Couldn't transfer %s to %s", xf.name, self.ip_addr)
                    # TODO: delete computation?
                sock.close()

            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock = AsynCoroSocket(sock, blocking=False,
                                  keyfile=self.keyfile, certfile=self.certfile)
            sock.settimeout(3)
            yield sock.connect((self.scheduler_ip_addr, self.scheduler_port))
            req = 'ADD_COMPUTE:' + cPickle.dumps({'ID':self._compute.id})
            yield sock.write(self.auth_code)
            yield sock.write_msg(req)
            msg = yield sock.read_msg()
            sock.close()
            resp = cPickle.loads(msg)
            if resp == self._compute.id:
                logging.debug('Computation %s created with %s', self._compute.name, self._compute.id)
                yield self._cluster.add_cluster(self, coro=coro)
            else:
                raise Exception('Computation "%s" could not be sent to scheduler' % self._compute.name)
        Coro(_add_compute, self).value()

    def submit(self, *args, **kwargs):
        """Submit a job for execution with the given arguments.

        Arguments should be serializable and should correspond to
        arguments for computation used when cluster is created.
        """
        if self._compute.type == _Compute.prog_type:
            if kwargs:
                logging.warning("Programs can't have keyword arguments")
                return None
            args = [str(arg) for arg in args]
        try:
            _job = _DispyJob_(self._compute.id, args, kwargs)
        except:
            logging.warning('Creating job for "%s", "%s" failed with "%s"',
                            str(args), str(kwargs), traceback.format_exc())
            return None

        def _submit(self, _job, coro=None):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock = AsynCoroSocket(sock, blocking=False,
                                  keyfile=self.keyfile, certfile=self.certfile)
            sock.settimeout(5)
            yield sock.connect((self.scheduler_ip_addr, self.scheduler_port))
            req = 'JOB:' + cPickle.dumps(_job)
            yield sock.write(self.auth_code)
            yield sock.write_msg(req)
            msg = yield sock.read_msg()
            _job.uid = cPickle.loads(msg)
            self._cluster._sched_cv.acquire()
            self._cluster._sched_jobs[_job.uid] = _job
            self._pending_jobs += 1
            self._complete.clear()
            self._cluster._sched_cv.release()
            yield sock.write_msg('ACK')
            sock.close()
            if self.fault_recover_file:
                # TODO: reply may come back before we store it!
                self.store_job_info(_job)
            yield _job.job

        try:
            job = Coro(_submit, self, _job).value()
            return job
        except:
            logging.warning('Creating job for "%s", "%s" failed with "%s"',
                            str(args), str(kwargs), traceback.format_exc())
            _job.job._dispy_job_ = None
            del _job.job
            return None

    def store_job_info(self, _job):
        if not self.fault_recover_file:
            return
        self._cluster.fault_recover_lock.acquire()
        shelf = shelve.open(self.fault_recover_file, flag='c')
        state = {'id':_job.job.id, 'hash':_job.hash, 'compute_id':_job.compute_id,
                 'args':_job.args, 'kwargs':_job.kwargs,
                 'ip_addr':self.scheduler_ip_addr, 'port':self.scheduler_port}
        shelf[str(_job.uid)] = shelve_repr(state)
        shelf.close()
        self._cluster.fault_recover_lock.release()
        return

    def cancel(self, job):
        def _cancel(self, job, coro=None):
            self._cluster._sched_cv.acquire()
            _job = job._dispy_job_
            if _job is None or self._cluster._clusters.get(_job.compute_id, None) != self:
                logging.warning('Invalid job %s for cluster "%s"!', job.id, self._compute.name)
                self._cluster._sched_cv.release()
                yield -1; raise StopIteration
            if job.status not in [DispyJob.Created, DispyJob.ProvisionalResult]:
                logging.warning('Job %s is not valid for cancel (%s)', job.id, job.status)
                self._cluster._sched_cv.release()
                yield -1; raise StopIteration

            job.status = DispyJob.Cancelled
            assert self._pending_jobs >= 1
            yield self._cluster._sched_cv.release()
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock = AsynCoroSocket(sock, blocking=False,
                                  keyfile=self.keyfile, certfile=self.certfile)
            sock.settimeout(5)
            try:
                yield sock.connect((self.scheduler_ip_addr, self.scheduler_port))
                req = 'TERMINATE_JOB:' + cPickle.dumps(_job)
                yield sock.write(self.auth_code)
                yield sock.write_msg(req)
                logging.debug('Job %s is cancelled', _job.uid)
            except:
                logging.warning('Connection to scheduler failed: %s', traceback.format_exc())
                yield -1; raise StopIteration
            finally:
                sock.close()

            yield 0

        return Coro(_cancel, self, job).value()

    def close(self):
        self._complete.wait()
        Coro(self._cluster.del_cluster, self).value()

def fault_recover_jobs(fault_recover_file, port=51348,
                       secret='', certfile=None, keyfile=None, ip_addr=None):
    """Recover results of jobs submitted. If dispy client is
    unexpectedly terminated (e.g., due to exceptions), and dispy
    client was earlier started with 'fault_recover_file' option, the
    results of jobs submitted can be recovered with this function.

    NB: This function must NOT be used when a JobCluster using same
    fault_recover_file is running.

    @fault_recover_file is path to file in which dispy stored
        information about jobs. Once results are retrived, information
        about those jobs are removed, so results can't be retrieved
        more than once.

    @port is where node (or dispyscheduler in the case of
        SharedJobCluster) are available. By default this is 51348 in
        case of JobClustr/dispynode and 51347 in the case of
        SharedJobCluster/dispyscheduler. When recovering jobs for
        SharedJobCluster, the port must be set explicitly as default
        value here is meant for JobCluster.

    @ip_addr is IP address to use for this client, in case multiple
        network interfaces have been configured. Default is to use IP
        address associated with the 'hostname'.

    @secret is a string that is (hashed and) used for handshaking
        of communication with nodes.

    @certfile is path to file containing SSL certificate (see
        Python 'ssl' module).

    @keyfile is path to file containing private key for SSL
        communication (see Python 'ssl' module). This key may be
        stored in 'certfile' itself, in which case this should be
        None.
    """

    shelf = shelve.open(fault_recover_file, flag='w')

    if not ip_addr:
        ip_addr = socket.gethostbyname(socket.gethostname())

    srv_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv_sock.settimeout(2)
    srv_sock.bind((ip_addr, 0))

    port_req = cPickle.dumps({'ip_addr':ip_addr, 'port':srv_sock.getsockname()[1]})
    node_infos = {}
    for uid in shelf:
        job_info = shelf[uid]
        if job_info['ip_addr'] in node_infos:
            continue

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for x in xrange(5):
            sock.sendto('SERVERPORT:' + port_req, (job_info['ip_addr'], port))
            try:
                reply, addr = srv_sock.recvfrom(1024)
                reply = cPickle.loads(reply)
                # print 'Port for %s is: %s' % (reply['ip_addr'], reply['port'])
                if reply['version'] != _dispy_version:
                    raise Exception('peer version "%s" is different from dispy version "%s"' % \
                                    reply['version'], _dispy_version)
                auth_code = hashlib.sha1(_xor_string(reply['sign'], secret)).hexdigest()
                if reply['ip_addr'] in node_infos:
                    continue
                node_infos[reply['ip_addr']] = {'port':reply['port'], 'auth_code':auth_code}
                if reply['ip_addr'] == job_info['ip_addr']:
                    break
            except:
                # print traceback.format_exc()
                pass
        else:
            print 'Could not get server port information from %s' % job_info['ip_addr']
        sock.close()
    srv_sock.close()

    jobs = []
    uids = shelf.keys()
    for uid in uids:
        job_info = shelf[uid]
        node_info = node_infos.get(job_info['ip_addr'], None)
        if node_info is None:
            continue
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock = AsynCoroSocket(sock, blocking=True, keyfile=keyfile, certfile=certfile)
        sock.settimeout(2)
        try:
            sock.connect((job_info['ip_addr'], node_info['port']))
            req = 'RETRIEVE_JOB:' + cPickle.dumps({'uid':int(uid), 'hash':job_info['hash'],
                                                   'compute_id':job_info['compute_id']})
            sock.write(node_info['auth_code'])
            sock.write_msg(req)
            resp = sock.read_msg()
            sock.write_msg('ACK')
            reply = cPickle.loads(resp)
            if not isinstance(reply, _JobReply):
                print 'Failed to get reply for %s: %s' % (uid, reply)
                continue
            job = DispyJob()
            job.id = job_info['id']
            job.status = reply.status
            job.result = reply.result
            job.stdout = reply.stdout
            job.stderr = reply.stderr
            job.exception = reply.exception
            job.ip_addr = job_info['ip_addr']
            setattr(job, 'args', cPickle.loads(job_info['args']))
            setattr(job, 'kwargs', cPickle.loads(job_info['kwargs']))
            jobs.append(job)
            if job.status in [DispyJob.Finished, DispyJob.Terminated]:
                del shelf[uid]
        except:
            print 'Failed to get reply for %s' % (uid)
            # print traceback.format_exc()
        finally:
            sock.close()

    pending = len(shelf)
    shelf.close()
    if pending == 0:
        # depending on db used, the file may have extension that
        # apparently is not easy to determine, so removing the file
        # may fail
        try:
            os.remove(fault_recover_file)
        except:
            pass
            # print 'Could not remove file "%s"' % fault_recover_file
    return jobs

if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument('computation', help='program to distribute and parallelize')
    parser.add_argument('-c', action='store_false', dest='cleanup', default=True,
                        help='if True, nodes will remove any files transferred when this computation is over')
    parser.add_argument('-d', action='store_true', dest='loglevel', default=False,
                        help='if True, debug messages are printed')
    parser.add_argument('-a', action='append', dest='args', default=[],
                        help='argument(s) to program; repeat for multiple instances')
    parser.add_argument('-f', action='append', dest='depends', default=[],
                        help='dependencies (files) needed by program')
    parser.add_argument('-n', '--nodes', action='append', dest='nodes', default=[],
                        help='list of nodes (names or IP address) acceptable for this computation')
    parser.add_argument('--secret', dest='secret', default='',
                        help='authentication secret for handshake with nodes')
    parser.add_argument('--certfile', dest='certfile', default=None,
                        help='file containing SSL certificate')
    parser.add_argument('--keyfile', dest='keyfile', default=None,
                        help='file containing SSL key')
    config = vars(parser.parse_args(sys.argv[1:]))
    # print config

    if config['loglevel']:
        config['loglevel'] = logging.DEBUG
    else:
        config['loglevel'] = logging.INFO

    args = config.pop('args')
    cluster = JobCluster(**config)
    for arg in args:
        cluster.submit(*arg)
    cluster.wait()
    cluster.stats()
    exit(0)
