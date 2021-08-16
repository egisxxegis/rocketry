
from powerbase.core.conditions import AlwaysTrue, AlwaysFalse, All
from powerbase.core.log import TaskAdapter
from powerbase.core.conditions import set_statement_defaults, BaseCondition
from powerbase.core.utils import is_pickleable
from powerbase.core.exceptions import SchedulerRestart, TaskInactionException, TaskTerminationException
from powerbase.log import QueueHandler

from .utils import get_execution, get_dependencies

from powerbase.conditions import DependSuccess
from powerbase.core.parameters import Parameters

import os, time
import platform
import logging
import inspect
import warnings
import datetime
from typing import List, Dict, Union, Tuple
import multiprocessing
import threading
from queue import Empty

from functools import wraps
from copy import copy
from itertools import count

import pandas as pd

CLS_TASKS = {}

_IS_WINDOWS = platform.system()

def register_task_cls(cls):
    """Add Task class to registered
    Task dict in order to initiate
    it from configuration"""
    CLS_TASKS[cls.__name__] = cls
    return cls

class _TaskMeta(type):
    def __new__(mcs, name, bases, class_dict):

        cls = type.__new__(mcs, name, bases, class_dict)

        # Store the name and class for configurations
        is_private = name.startswith("_")
        is_base = name == "Task"
        if not is_private and not is_base:
            if cls.session is not None and cls.session.config["session_store_task_cls"]:
                cls.session.task_cls[cls.__name__] = cls
        return cls


class Task(metaclass=_TaskMeta):
    """Executable task 

    This class is meant to be container
    for all the information needed to run
    the task.

    To subclass:
    ------------
        - Put task specific parameters to __init__ 
          and session/scheduler specific to 
          execute_action. 
          Remember to call super().__init__.

    Public attributes:
    ------------------
        action {function} : Function to execute as task. Parameters are passed by scheduler

        start_cond {Condition} : Condition to start the task (bool returns True)
        run_cond {Condition} : If condition returns False when task is running, termination occurs (only applicable with multiprocessing/threading)
        end_cond {Condition} : If condition returns True when task is running, termination occurs (only applicable with multiprocessing/threading)
        timeout {int, float} : Seconds allowed to run or terminated (only applicable with multiprocessing/threading)

        priority {int} : Priority of the task. Higher priority tasks are run first

        on_failure {function} : Function to execute if the action raises exception
        on_success {function} : Function to execute if the action succeessed
        on_finish {function} : Function to execute when the function finished (failed or success)

        dependent {List[str]} : List of task names to must run before this task (in their execution cycle)

        name {str, tuple} : Name of the task. Must be unique
        groups {tuple} : Name of the group the task is part of. Different groups use different loggers

        force_run {bool} : Force the task to be run once (if True)
        disabled {bool} : Force the task to not to be run (force_run overrides disabling)

        execution {str} : How to execute the task. Options: {single, process, thread}

    Readonly Properties:
    -----------
        is_running -> bool : Check whether the task is currently running or not
        status -> str : Latest status of the task

    Class attributes:
    -----------------
        use_instance_naming {bool} : if task instance has specified no name, use id(task) as the name
        permanent_task {bool} : Whether the task is meant to be run indefinitely. Use in subclassing.
        status_from_logs {bool} : Whether to check the task.status from the logs (True) or store to memory (False)
        on_exists {str} : If task already exists, 
            raise an exception (on_exists="raise")
            or replace the old task (on_exists="replace")
            or generate new name for the new task (on_exists="rename")
            or don't overwrite the existing task (on_exists="ignore")

    Methods:
    --------
        __call__(*args, **kwargs) : Execute the task
        __bool__() : Whether the task can be run now or not
        filter_params(params:Dict) : Filter the passed parameters needed by the action

        log_running() : Log that the task is running
        log_failure() : Log that the task has failed
        log_success() : Log that the task has succeeded
        log_termination() : Log that the task has been terminated
        log_inaction() : Log that the task has not really done anything


    """
    use_instance_naming = False
    permanent_task = False # Whether the task is not meant to finish (Ie. RestAPI)
    _actions = ("run", "fail", "success", "inaction", "terminate", None, "crash_release")

    # For multiprocessing (if None, uses scheduler's default)
    daemon: bool = None

    disabled: bool
    force_run: bool
    name: str
    priority: int

    session = None
    # TODO:
    #   remove "run_cond"

    def __init__(self, parameters=None, session=None,
                start_cond=None, run_cond=None, end_cond=None, 
                dependent=None, timeout=None, priority=1, 
                on_success=None, on_failure=None, on_finish=None, 
                name=None, logger=None, 
                execution="process", disabled=False, force_run=False,
                on_startup=False, on_shutdown=False):
        """[summary]

        Arguments:
            condition {[type]} -- [description]
            action {[type]} -- [description]

        Keyword Arguments:
            priority {int} -- [description] (default: {1})
            on_success {[func]} -- Function to run on success (default: {None})
            on_failure {[func]} -- Function to run on failure (default: {None})
            on_finish {[func]} -- Function to run after running the task (default: {None})

            on_exists ([str]) -- What to do if task (with same name) has already been created. (Options: 'raise', 'ignore', 'replace')
        """
        self.session = self.session or session

        self.name = name
        self.logger = logger
        
        self.status = None

        self.start_cond = AlwaysFalse() if start_cond is None else copy(start_cond) # If no start_condition, won't run except manually
        self.run_cond = AlwaysTrue() if run_cond is None else copy(run_cond)
        self.end_cond = AlwaysFalse() if end_cond is None else copy(end_cond)

        self.timeout = (
            pd.Timedelta.max # "never" is 292 years
            if timeout == "never"
            else pd.Timedelta(timeout)
            if timeout is not None 
            else timeout
        )
        self.priority = priority

        self.execution = execution
        self.on_startup = on_startup
        self.on_shutdown = on_shutdown

        self.disabled = disabled
        self.force_run = force_run
        self.force_termination = False

        self.on_failure = on_failure
        self.on_success = on_success
        self.on_finish = on_finish

        self.dependent = dependent
        self.parameters = parameters

        # Thread specific (readonly properties)
        self._thread_terminate = threading.Event()
        self._lock = threading.Lock() # So that multiple threaded tasks/scheduler won't simultaneusly use the task

        # Whether the task is maintenance task
        self.is_maintenance = False

        self._set_default_task()

    @property
    def start_cond(self):
        return self._start_cond
    
    @start_cond.setter
    def start_cond(self, cond):
        # Rare exception: We need something from builtins (outside core) to be user friendly
        from powerbase.parse.condition import parse_condition
        cond = parse_condition(cond) if isinstance(cond, str) else cond
        self._validate_cond(cond)

        set_statement_defaults(cond, task=self)
        self._start_cond = cond
        
    @property
    def end_cond(self):
        return self._end_cond
    
    @end_cond.setter
    def end_cond(self, cond):
        # Rare exception: We need something from builtins (outside core) to be user friendly
        from powerbase.parse.condition import parse_condition
        cond = parse_condition(cond) if isinstance(cond, str) else cond
        self._validate_cond(cond)

        set_statement_defaults(cond, task=self)
        self._end_cond = cond

    @property
    def dependent(self):
        return get_dependencies(self)

    @dependent.setter
    def dependent(self, tasks:list):
        # tasks: List[str]
        if not tasks:
            # TODO: Remove dependent parts
            return
        dep_cond = All(*(DependSuccess(depend_task=task, task=self.name) for task in tasks))
        self.start_cond &= dep_cond

    @property
    def parameters(self):
        return self._parameters

    @parameters.setter
    def parameters(self, val):
        if val is None:
            self._parameters = Parameters()
        else:
            self._parameters = Parameters(val)

    def _validate_cond(self, cond):
        if not isinstance(cond, (BaseCondition, bool)):
            raise TypeError(f"Condition must be bool or inherited from {BaseCondition}. Given: {type(cond)}")

    def _set_default_task(self):
        "Set the task in subconditions that are missing "
        set_statement_defaults(self.start_cond, task=self)
        set_statement_defaults(self.run_cond, task=self)
        set_statement_defaults(self.end_cond, task=self)

    def __call__(self, **kwargs):
        # Remove old threads/processes
        # (using _process and _threads are most robust way to check if running as process or thread)
        if hasattr(self, "_process"):
            del self._process
        if hasattr(self, "_thread"):
            del self._thread

        # Run the actual task
        if self.execution == "main":
            self.run_as_main(**kwargs)
            if _IS_WINDOWS:
                # There is an annoying bug (?) in Windows:
                # https://bugs.python.org/issue44831
                # If one checks whether the task has succeeded/failed
                # already the log might show that the task finished 
                # 1 microsecond in the future if memory logging is used. 
                # Therefore we sleep that so condition checks especially 
                # in tests will succeed. 
                time.sleep(1e-6)
        elif self.execution == "process":
            self.run_as_process(**kwargs)
        elif self.execution == "thread":
            self.run_as_thread(**kwargs)
        else:
            raise ValueError(f"Invalid execution: {self.execution}")

    def __bool__(self):
        "Check whether the task can be run or not"
        # TODO: rename force_run to forced_state that can be set to False (will not run any case) or True (will run once any case)
        # Also add methods: 
        #    set_pending() : Set forced_state to False
        #    resume() : Reset forced_state to None
        #    set_running() : Set forced_state to True

        if self.force_run:
            return True
        elif self.disabled:
            return False

        cond = bool(self.start_cond)

        return cond

    def run_as_main(self, params=None, _log_running=True, **kwargs):
        if _log_running:
            self.log_running()
        #self.logger.info(f'Running {self.name}', extra={"action": "run"})

        #old_cwd = os.getcwd()
        #if cwd is not None:
        #    os.chdir(cwd)

        # (If SystemExit is raised, it won't be catched in except Exception)
        status = None
        params = {} if params is None else params
        try:
            params = Parameters(params)

            # We filter only the non-explicit parameters (session parameters)
            params = self.filter_params(params)
            params = Parameters(params)

            params.update(self.parameters) # Union setup params with call params
            params = params.materialize()

            output = self.execute_action(**params)

        except SchedulerRestart:
            # SchedulerRestart is considered as successful task
            self.log_success()
            #self.logger.info(f'Task {self.name} succeeded', extra={"action": "success"})
            status = "succeeded"
            self.process_success(output)
            # TODO: Probably should raise and not silently return?
            raise

        except TaskInactionException:
            # Task did not fail, it did not succeed:
            #   The task started but quickly determined was not needed to be run
            #   and therefore the purpose of the task was not executed.
            self.log_inaction()
            status = "inaction"
            
        except TaskTerminationException:
            # Task was terminated and the task's function
            # did listen to that.
            self.log_termination()
            status = "termination"

        except Exception as exception:
            # All the other exceptions (failures)
            self.log_failure()
            status = "failed"
            self.process_failure(exception=exception)
            #self.logger.error(f'Task {self.name} failed', exc_info=True, extra={"action": "fail"})

            self.exception = exception
            raise

        else:
            self.log_success()
            #self.logger.info(f'Task {self.name} succeeded', extra={"action": "success"})
            status = "succeeded"
            self.process_success(output)
            
            return output

        finally:
            self.process_finish(status=status)
            self.force_run = None
            #if cwd is not None:
            #    os.chdir(old_cwd)

    def run_as_thread(self, params=None, **kwargs):
        "Create thread and run the task in it"
        self.thread_terminate.clear()

        event_is_running = threading.Event()
        self._thread = threading.Thread(target=self._run_as_thread, args=(params, event_is_running))
        self.start_time = datetime.datetime.now() # Needed for termination
        self._thread.start()
        event_is_running.wait() # Wait until the task is confirmed to run 
 

    def _run_as_thread(self, params=None, event=None):
        "Run the task in the thread itself"
        self.log_running()
        event.set()
        # Adding the _thread_terminate as param so the task can
        # get the signal for termination
        params = Parameters() if params is None else params
        params = params | {"_thread_terminate_": self._thread_terminate}
        self.run_as_main(params=params, _log_running=False)

    def run_as_process(self, params=None, log_queue=None, return_queue=None, daemon=None):
        # Daemon resolution: task.daemon >> scheduler.tasks_as_daemon
        if not log_queue:
            log_queue = multiprocessing.Queue(-1)
        daemon = self.daemon if self.daemon is not None else daemon
        self._process = multiprocessing.Process(target=self._run_as_process, args=(log_queue, return_queue, params), daemon=daemon) 
        self.start_time = datetime.datetime.now() # Needed for termination
        self._process.start()
        
        self._lock_to_run_log(log_queue)
        return log_queue

    def _run_as_process(self, queue, return_queue, params):
        """Run a task in a separate process (has own memory)"""

        # NOTE: This is in the process and other info in the application
        # cannot be accessed here. Self is a copy of the original
        # and cannot affect main processes' attributes!
        
        # The task's logger has been removed by MultiScheduler.run_task_as_process
        # (see the method for more info) and we need to recreate the logger now
        # in the actual multiprocessing's process. We only add QueueHandler to the
        # logger (with multiprocessing.Queue as queue) so that all the logging
        # records end up in the main process to be logged properly. 

        basename = self.session.config["task_logger_basename"]
        # handler = logging.handlers.QueueHandler(queue)
        handler = QueueHandler(queue)

        # Set the process logger
        logger = logging.getLogger(basename + "._process")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        logger.handlers = []
        logger.addHandler(handler)
        try:
            with warnings.catch_warnings():
                # task.set_logger will warn that 
                # we do not use two-way logger here 
                # but that is not needed as running 
                # the task itself does not require
                # knowing the status of the task
                # or other tasks
                warnings.simplefilter("ignore")
                self.logger = logger
            #task.logger.addHandler(
            #    logging.StreamHandler(sys.stdout)
            #)
            #task.logger.addHandler(
            #    QueueHandler(queue)
            #)
        except:
            logger.critical(f"Task '{self.name}' crashed in setting up logger.", exc_info=True, extra={"action": "fail", "task_name": self.name})
            raise

        try:
            # NOTE: The parameters are "materialized" 
            # here in the actual process that runs the task
            output = self.run_as_main(params=params)
        except Exception as exc:
            # Just catching all exceptions.
            # There is nothing to raise it
            # to :(
            pass
        else:
            if return_queue:
                return_queue.put((self.name, output))

    def filter_params(self, params):
        "By default, filter keyword arguments required by self.execute_action"
        sig = inspect.signature(self.execute_action)
        kw_args = [
            val.name
            for name, val in sig.parameters.items()
            if val.kind in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD, # Normal argument
                inspect.Parameter.KEYWORD_ONLY # Keyword argument
            )
        ]
        return {
            key: val for key, val in params.items()
            if key in kw_args
        }

    def execute_action(self, *args, **kwargs):
        "Run the actual, given, task"
        raise NotImplementedError(f"Method 'execute_action' not implemented to {type(self)}")

    def process_failure(self, exception):
        if self.on_failure:
            self.on_failure(exception=exception)
    
    def process_success(self, output):
        if self.on_success:
            self.on_success(output)

    def process_finish(self, status):
        if self.on_finish:
            self.on_finish(status)

    @property
    def is_running(self):
        return self.status == "run"

    @property
    def name(self):
        return self._name
    
    @name.setter
    def name(self, name):
        # TODO: Change the name in _TASKS
        old_name = None if not hasattr(self, "_name") else self._name

        if name is None:
            name = (
                id(self)
                if self.session.config["use_instance_naming"]
                else self.get_default_name()
            )

        if name == old_name:
            return
        
        if name in self.session.tasks:
            on_exists = self.session.config["on_task_pre_exists"]
            if on_exists == "replace":
                self.session.tasks[name] = self
            elif on_exists == "raise":
                raise KeyError(f"Task {name} already exists. (All tasks: {self.session.tasks})")
            elif on_exists == "ignore":
                pass
            elif on_exists == "rename":
                for i in count():
                    new_name = name + str(i)
                    if new_name not in self.session.tasks:
                        self.name = new_name
                        return
        else:
            self.session.tasks[name] = self
        
        self._name = str(name)

        if old_name is not None:
            del self.session.tasks[old_name]

    def get_default_name(self):
        raise NotImplementedError(f"Method 'get_default_name' not implemented to {type(self)}")

    def is_alive(self):
        return self.is_alive_as_thread() or self.is_alive_as_process()

    def is_alive_as_thread(self):
        return hasattr(self, "_thread") and self._thread.is_alive()

    def is_alive_as_process(self):
        return hasattr(self, "_process") and self._process.is_alive()
        
# Logging
    def _lock_to_run_log(self, log_queue):
        "Handle next run log to make sure the task started running before continuing (otherwise may cause accidential multiple launches)"
        action = None
        timeout = 10 # Seconds allowed the setup to take before declaring setup to crash
        #record = log_queue.get(block=True, timeout=None)
        while action != "run":
            try:
                record = log_queue.get(block=True, timeout=timeout)
            except Empty:
                if not self.is_alive():
                    # There will be no "run" log record thus ending the task gracefully
                    self.logger.critical(f"Task '{self.name}' crashed in setup", extra={"action": "fail"})
                    return
            else:
                
                self.logger.debug(f"Inserting record for '{record.task_name}' ({record.action})")
                task = self.session.get_task(record.task_name)
                task.log_record(record)

                action = record.action

    @property
    def logger(self):
        return self._logger

    @logger.setter
    def logger(self, logger):
        basename = self.session.config["task_logger_basename"]
        if logger is None:
            # Get class logger (default logger)
            logger = logging.getLogger(basename)
        if isinstance(logger, str):
            logger = logging.getLogger(logger)

        if not logger.name.startswith(basename):
            raise ValueError(f"Logger name must start with '{basename}' as session finds loggers with names")

        if not isinstance(logger, TaskAdapter):
            logger = TaskAdapter(logger, task=self)
        self._logger = logger

    def log_running(self):
        self.status = "run"

    def log_failure(self):
        self.status = "fail", f"Task '{self.name}' failed"

    def log_success(self):
        self.status = "success"

    def log_termination(self, reason=None):
        reason = reason or "unknown reason"
        self.status = "terminate", reason

        # Reset event and force_termination (for threads)
        self.thread_terminate.clear()
        self.force_termination = False

    def log_inaction(self):
        self.status = "inaction"

    def log_record(self, record):
        "For multiprocessing in which the record goes from copy of the task to scheduler before it comes back to the original task"
        self.logger.handle(record)
        self._status = record.action

    @property
    def status(self):
        if self.session.config["force_status_from_logs"]:
            try:
                record = self.logger.get_latest()
            except AttributeError:
                warnings.warn(f"Task '{self.name}' logger is not readable. Status unknown.")
                record = None
            if not record:
                # No previous status
                return None
            return record["action"]
        else:
            # This is way faster
            return self._status

    @status.setter
    def status(self, value:Union[str, Tuple[str, str]]):
        "Set status (and log the status) of the scheduler"
        if isinstance(value, tuple):
            action = value[0]
            message = value[1]
        else:
            action = value
            message = ""
        if action not in self._actions:
            raise KeyError(f"Invalid action: {action}")
        
        if action is not None:
            now = datetime.datetime.now()
            if action == "run":
                extra = {"action": "run", "start": now}
                self.start_time = now
            else:
                start_time = self.start_time if hasattr(self, "start_time") else None
                runtime = now - start_time if start_time is not None else None
                extra = {"action": action, "start": start_time, "end": now, "runtime": runtime}
            
            log_method = self.logger.exception if action == "fail" else self.logger.info
            log_method(
                message, 
                extra=extra
            )
        self._status = action
            

    def get_history(self) -> List[Dict]:
        records = self.logger.get_records()
        return records

    def __getstate__(self):

        # capture what is normally pickled
        state = self.__dict__.copy()

        # remove unpicklable
        # TODO: Include conditions by enforcing tasks are passed to the conditions as names
        state['_logger'] = None
        state['_start_cond'] = None
        state['_end_cond'] = None
        state["_process"] = None # If If execution == "process"
        state["_thread"] = None # If execution == "thread"

        state["_thread_terminate"] = None # Event only for threads

        state["_lock"] = None # Process task cannot lock anything anyways

        # what we return here will be stored in the pickle
        return state

    @property
    def thread_terminate(self):
        "Event to signal terminating the threaded task"
        # Readonly "attribute"
        return self._thread_terminate

    @property
    def lock(self):
        return self._lock

# Other
    @property
    def period(self):
        "Determine Time object for the interval (maximum possible if time independent as 'or')"
        return get_execution(self)