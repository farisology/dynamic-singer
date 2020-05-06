import json
import os
import re
import sys
import singer
from subprocess import Popen, PIPE, STDOUT
from dynamic_singer import helper, function
from typing import Callable, Dict
from herpetologist import check_type
from tornado import gen
from prometheus_client import start_http_server, Counter, Summary, Histogram
import logging

logger = logging.getLogger(__name__)


@gen.coroutine
def _sinking(line, target):
    if isinstance(target.target, Popen):
        target.target.stdin.write('{}\n'.format(line).encode())
        target.target.stdin.flush()

        r = []
        while True:
            output = function.non_block_read(target.target.stdout).strip()
            if len(output):
                r.append(output.decode().strip())
            else:
                break
        r = '\n'.join(r)

    else:
        r = target.target.parse(line)

    target._tap_count.inc()
    target._tap_data.observe(sys.getsizeof(r) / 1000)
    target._tap_data_histogram.observe(sys.getsizeof(r) / 1000)
    return r


class Source:
    @check_type
    def __init__(
        self,
        tap,
        tap_schema: Dict = None,
        tap_name: str = None,
        tap_key: str = None,
        port: int = 8000,
    ):

        if not isinstance(tap, str) and not hasattr(tap, 'emit'):
            raise ValueError(
                'tap must a string or an object with method `emit`'
            )
        if isinstance(tap, Callable):
            self.tap = helper.Tap(
                tap,
                tap_schema = tap_schema,
                tap_name = tap_name,
                tap_key = tap_key,
            )
            f = tap_name
        else:
            self.tap = tap
            f = tap
        self._targets = []
        start_http_server(port)
        f = function.parse_name(f)

        self._tap_count = Counter(f'total_{f}', f'total rows {f}')
        self._tap_data = Summary(
            f'data_size_{f}', f'summary of data size {f} (KB)'
        )
        self._tap_data_histogram = Histogram(
            f'data_size_histogram_{f}', f'histogram of data size {f} (KB)'
        )

    def add(self, target):
        if not isinstance(target, str) and not hasattr(target, 'parse'):
            raise ValueError(
                'target must a string or an object with method `parse`'
            )

        if isinstance(target, str):
            if '.py' in target:
                target = f'python3 {target}'
        self._targets.append(target)

    def get_targets(self):
        return self._targets

    @check_type
    def delete_target(self, index: int):
        self._targets.pop(index)

    @check_type
    def start(self, debug = True, asynchronous: bool = False):

        if not len(self._targets):
            raise Exception(
                'targets are empty, please add a target using `source.add()` first.'
            )
        self._pipes = []
        for target in self._targets:
            if isinstance(target, str):
                p = Popen(
                    target.split(), stdout = PIPE, stdin = PIPE, stderr = PIPE
                )
                t = helper.Check_Error(p)
                t.start()
            else:
                p = target

            self._pipes.append(helper.Target(p, target))

        if isinstance(self.tap, str):
            pse = Popen(
                self.tap.split(), stdout = PIPE, stdin = PIPE, stderr = PIPE
            )
            t = helper.Check_Error(pse)
            t.start()

            pse = iter(pse.stdout.readline, b'')
        else:
            pse = self.tap

        for line in pse:
            line = line.decode().strip()
            if len(line):
                if debug:
                    logger.info(line)

                self._tap_count.inc()
                self._tap_data.observe(sys.getsizeof(line) / 1000)
                self._tap_data_histogram.observe(sys.getsizeof(line) / 1000)

                if asynchronous:

                    @gen.coroutine
                    def loop():
                        r = yield [_sinking(line, pipe) for pipe in self._pipes]

                    result = loop()
                    if debug:
                        logger.info(result.result())

                else:
                    for pipe in self._pipes:
                        result = _sinking(line, pipe)
                        if debug:
                            logger.info(result.result())

        for pipe in self._pipes:
            if isinstance(pipe.target, Popen):
                try:
                    pipe.target.communicate()
                except:
                    pass
