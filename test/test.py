#!/usr/bin/env python

import os
from os import path
import sys
import time
import uuid
import signal
import colorama
from colorama import Fore, Back, Style
from parse_arguments import parse_arguments
import project_root
from helpers.helpers import (
    Popen, PIPE, check_call, TMPDIR, parse_config, kill_proc_group
    timeout_handler, TimeoutError, format_time)
from test_helpers import (
    read_port_from_proc, who_runs_first, parse_remote_path, query_clock_offset)


class Test(object):
    def __init__(self, args, run_id, cc):
        self.mode = args.mode
        self.run_id = run_id
        self.cc = cc

        # shared arguments between local and remote modes
        self.flows = args.flows
        self.runtime = args.runtime
        self.interval = args.interval

        # local mode
        if self.mode == 'local':
            self.datalink_trace = args.uplink_trace
            self.acklink_trace = args.downlink_trace
            self.prepend_mm_cmds = args.prepend_mm_cmds
            self.append_mm_cmds = args.append_mm_cmds
            self.extra_mm_link_args = args.extra_mm_link_args

            # for convenience
            self.sender_side = 'remote'
            self.server_side = 'local'

        # remote mode
        if self.mode == 'remote':
            self.sender_side = args.sender_side
            self.server_side = args.server_side
            self.local_addr = args.local_addr
            self.local_if = args.local_if
            self.remote_if = args.remote_if
            self.local_desc = args.local_desc
            self.remote_desc = args.remote_desc
            self.ntp_addr = args.ntp_addr
            self.worst_clock_offset = None

            self.r = parse_remote_path(args.remote_path, self.cc)

    def setup_mm_cmd(self):
        datalink_trace = path.join(self.test_dir, self.datalink_trace)
        acklink_trace = path.join(self.test_dir, self.acklink_trace)

        mm_datalink_log = self.cc + '_mm_datalink_run%d.log' % self.run_id
        mm_acklink_log = self.cc + '_mm_acklink_run%d.log' % self.run_id
        self.mm_datalink_log = path.join(self.test_dir, mm_datalink_log)
        self.mm_acklink_log = path.join(self.test_dir, mm_acklink_log)

        if self.run_first == 'receiver' or self.flows > 0:
            # if receiver runs first OR if test inside pantheon tunnel
            uplink_log = self.mm_datalink_log
            downlink_log = self.mm_acklink_log
            uplink_trace = datalink_trace
            downlink_trace = acklink_trace
        else:
            # if sender runs first AND test without pantheon tunnel
            uplink_log = self.mm_acklink_log
            downlink_log = self.mm_datalink_log
            uplink_trace = acklink_trace
            downlink_trace = datalink_trace

        self.mm_cmd = []

        if self.prepend_mm_cmds:
            self.mm_cmd += self.prepend_mm_cmds.split()

        self.mm_cmd += [
            'mm-link', uplink_trace, downlink_trace,
            '--uplink-log=' + uplink_log,
            '--downlink-log=' + downlink_log]

        if self.extra_mm_link_args:
            self.mm_cmd += self.extra_mm_link_args.split()

        if self.append_mm_cmds:
            self.mm_cmd += self.append_mm_cmds.split()

    def setup(self):
        # setup commonly used paths
        self.cc_src = path.join(project_root.DIR, 'src', self.cc + '.py')
        self.test_dir = path.join(project_root.DIR, 'test')
        self.tunnel_manager = path.join(self.test_dir, 'tunnel_manager.py')

        # record who runs first
        self.run_first, self.run_second = who_runs_first(self.cc)

        # wait for 2 seconds until run_first is ready
        self.run_first_setup_time = 2

        # setup output logs
        self.datalink_name = self.cc + '_datalink_run%d' % self.run_id
        self.acklink_name = self.cc + '_acklink_run%d' % self.run_id

        self.datalink_log = path.join(
            self.test_dir, self.datalink_name + '.log')
        self.acklink_log = path.join(
            self.test_dir, self.acklink_name + '.log')

        if self.flows > 0:
            self.datalink_ingress_logs = []
            self.datalink_egress_logs = []
            self.acklink_ingress_logs = []
            self.acklink_egress_logs = []

            for tun_id in xrange(1, self.flows + 1):
                uid = uuid.uuid4()

                self.datalink_ingress_logs.append(path.join(
                    TMPDIR, '%s_flow%s_uid%s.log.ingress'
                    % (self.datalink_name, tun_id, uid)))
                self.datalink_egress_logs.append(path.join(
                    TMPDIR, '%s_flow%s_uid%s.log.egress'
                    % (self.datalink_name, tun_id, uid)))
                self.acklink_ingress_logs.append(path.join(
                    TMPDIR, '%s_flow%s_uid%s.log.ingress'
                    % (self.acklink_name, tun_id, uid)))
                self.acklink_egress_logs.append(path.join(
                    TMPDIR, '%s_flow%s_uid%s.log.egress'
                    % (self.acklink_name, tun_id, uid)))

        if self.mode == 'local':
            self.setup_mm_cmd()
        else:
            # update worst (absolute) clock offset
            if self.ntp_addr is not None:
                offset = query_clock_offset(self.ntp_addr, self.r['ssh_cmd'])

                if (self.worst_clock_offset is None or
                        offset > self.worst_clock_offset):
                    self.worst_clock_offset = offset

    # test congestion control without running pantheon tunnel
    def run_without_tunnel(self):
        # run the side specified by self.run_first
        cmd = ['python', self.cc_src, self.run_first]
        sys.stderr.write('Running %s %s...\n' % (self.cc, self.run_first))
        proc_first = Popen(cmd, stdout=PIPE, preexec_fn=os.setsid)

        # find port printed
        port = None
        while port is None:
            port = read_port_from_proc(proc_first)

        # sleep just in case the process isn't quite listening yet
        # the cleaner approach might be to try to verify the socket is open
        time.sleep(self.run_first_setup_time)

        self.test_start_time = format_time()
        # run the other side specified by self.run_second
        sh_cmd = 'python %s %s $MAHIMAHI_BASE %s' % (
            self.cc_src, self.run_second, port)
        sh_cmd = ' '.join(self.mm_cmd) + " -- sh -c '%s'" % sh_cmd
        sys.stderr.write('Running %s %s...\n' % (self.cc, self.run_second))
        proc_second = Popen(sh_cmd, stdout=PIPE, shell=True,
                            preexec_fn=os.setsid)

        signal.signal(signal.SIGALRM, timeout_handler)
        signal.alarm(self.runtime)

        try:
            proc_first.communicate()
            proc_second.communicate()
        except TimeoutError:
            self.test_end_time = format_time()
        else:
            signal.alarm(0)
            sys.exit('Test exited before time limit')
        finally:
            kill_proc_group(proc_first, signal.SIGKILL)
            kill_proc_group(proc_second, signal.SIGKILL)

    def run_tunnel_managers(self):
        # run tunnel server manager
        if self.mode == 'remote':
            if self.server_side == 'local':
                ts_manager_cmd = ['python', self.tunnel_manager]
            else:
                ts_manager_cmd = self.r['ssh_cmd'] + [
                    'python', self.r['tunnel_manager']]
        else:
            ts_manager_cmd = ['python', self.tunnel_manager]

        sys.stderr.write(Fore.BLUE + '[tunnel server manager (tsm)] ' +
                         Style.RESET_ALL)
        ts_manager = Popen(ts_manager_cmd, stdin=PIPE, stdout=PIPE)

        while True:
            running = ts_manager.stdout.readline()
            if 'tunnel manager is running' in running:
                sys.stderr.write(running)
                break

        ts_manager.stdin.write('prompt [tsm]\n')

        # run tunnel client manager
        if self.mode == 'remote':
            if self.server_side == 'local':
                tc_manager_cmd = self.r['ssh_cmd'] + [
                    'python', self.r['tunnel_manager']]
            else:
                tc_manager_cmd = ['python', self.tunnel_manager]
        else:
            tc_manager_cmd = self.mm_cmd + ['python', self.tunnel_manager]

        sys.stderr.write(Fore.BLUE + '[tunnel client manager (tcm)] ' +
                         Style.RESET_ALL)
        tc_manager = Popen(tc_manager_cmd, stdin=PIPE, stdout=PIPE)

        while True:
            running = tc_manager.stdout.readline()
            if 'tunnel manager is running' in running:
                sys.stderr.write(running)
                break

        tc_manager.stdin.write('prompt [tcm]\n')

        return ts_manager, tc_manager

    def run_tunnel_server(self, tun_id, ts_manager):
        if self.server_side == self.sender_side:
            ts_cmd = 'mm-tunnelserver --ingress-log=%s --egress-log=%s' % (
                self.acklink_ingress_logs[tun_id - 1],
                self.datalink_egress_logs[tun_id - 1])
        else:
            ts_cmd = 'mm-tunnelserver --ingress-log=%s --egress-log=%s' % (
                self.datalink_ingress_logs[tun_id - 1],
                self.acklink_egress_logs[tun_id - 1])

        if self.mode == 'remote':
            if self.server_side == 'remote':
                if self.remote_if is not None:
                    ts_cmd += ' --interface=' + self.remote_if
            else:
                if self.local_if is not None:
                    ts_cmd += ' --interface=' + self.local_if

        ts_cmd = 'tunnel %s %s\n' % (tun_id, ts_cmd)
        ts_manager.stdin.write(ts_cmd)

        # read the command to run tunnel client
        readline_cmd = 'tunnel %s readline\n' % tun_id
        ts_manager.stdin.write(readline_cmd)

        cmd_to_run_tc = ts_manager.stdout.readline().split()
        return cmd_to_run_tc

    def run_tunnel_client(self, tun_id, tc_manager, cmd_to_run_tc):
        if self.mode == 'local':
            cmd_to_run_tc[1] = '$MAHIMAHI_BASE'
        else:
            if self.server_side == 'remote':
                cmd_to_run_tc[1] = self.r['ip']
            else:
                cmd_to_run_tc[1] = self.local_addr

        cmd_to_run_tc_str = ' '.join(cmd_to_run_tc)

        if self.server_side == self.sender_side:
            tc_cmd = '%s --ingress-log=%s --egress-log=%s' % (
                cmd_to_run_tc_str,
                self.datalink_ingress_logs[tun_id - 1],
                self.acklink_egress_logs[tun_id - 1])
        else:
            tc_cmd = '%s --ingress-log=%s --egress-log=%s' % (
                cmd_to_run_tc_str,
                self.acklink_ingress_logs[tun_id - 1],
                self.datalink_egress_logs[tun_id - 1])

        if self.mode == 'remote':
            if self.server_side == 'remote':
                if self.local_if is not None:
                    tc_cmd += ' --interface=' + self.local_if
            else:
                if self.remote_if is not None:
                    tc_cmd += ' --interface=' + self.remote_if

        tc_cmd = 'tunnel %s %s\n' % (tun_id, tc_cmd)
        readline_cmd = 'tunnel %s readline\n' % tun_id

        # re-run tunnel client after 20s timeout for at most 3 times
        max_run = 3
        curr_run = 0
        got_connection = ''
        while 'got connection' not in got_connection:
            curr_run += 1
            if curr_run > max_run:
                sys.exit('Cannot establish tunnel\n')

            tc_manager.stdin.write(tc_cmd)
            while True:
                tc_manager.stdin.write(readline_cmd)

                signal.signal(signal.SIGALRM, timeout_handler)
                signal.alarm(20)

                try:
                    got_connection = tc_manager.stdout.readline()
                    sys.stderr.write('Tunnel is connected\n')
                except TimeoutError:
                    sys.stderr.write('Tunnel connection timeout\n')
                    break
                else:
                    signal.alarm(0)
                    if 'got connection' in got_connection:
                        break

    def run_first_side(self, tun_id, send_manager, recv_manager,
                       send_pri_ip, recv_pri_ip):
        readline_cmd = 'tunnel %s readline\n' % tun_id

        first_src = self.cc_src
        second_src = self.cc_src

        if self.run_first == 'receiver':
            if self.mode == 'remote':
                if self.sender_side == 'local':
                    first_src = self.r['cc_src']
                else:
                    second_src = self.r['cc_src']

            first_cmd = 'tunnel %s python %s receiver\n' % (
                tun_id, first_src)
            second_cmd = 'tunnel %s python %s sender %s' % (
                tun_id, second_src, recv_pri_ip)

            recv_manager.stdin.write(first_cmd)

            # find printed port
            port = None
            while port is None:
                recv_manager.stdin.write(readline_cmd)
                port = read_port_from_proc(recv_manager)

            second_cmd += ' %s\n' % port
        else:  # self.run_first == 'sender'
            if self.mode == 'remote':
                if self.sender_side == 'local':
                    second_src = self.r['cc_src']
                else:
                    first_src = self.r['cc_src']

            first_cmd = 'tunnel %s python %s sender\n' % (
                tun_id, first_src)
            second_cmd = 'tunnel %s python %s receiver %s' % (
                tun_id, second_src, send_pri_ip)

            send_manager.stdin.write(first_cmd)

            # find printed port
            port = None
            while port is None:
                send_manager.stdin.write(readline_cmd)
                port = read_port_from_proc(send_manager)

            second_cmd += ' %s\n' % port

        return second_cmd

    def run_second_side(self, send_manager, recv_manager, second_cmds):
        time.sleep(self.run_first_setup_time)

        start_time = time.time()
        self.test_start_time = format_time()

        # start each flow self.interval seconds after the previous one
        for i in xrange(len(second_cmds)):
            if i != 0:
                time.sleep(self.interval)
            second_cmd = second_cmds[i]
            if self.run_first == 'receiver':
                send_manager.stdin.write(second_cmd)
            else:
                recv_manager.stdin.write(second_cmd)

        elapsed_time = time.time() - start_time
        if elapsed_time > self.runtime:
            sys.exit('Interval time between flows is too long')
        time.sleep(self.runtime - elapsed_time)

        self.test_end_time = format_time()

    # test congestion control using tunnel client and tunnel server
    def run_with_tunnel(self):
        # run pantheon tunnel server and client managers
        ts_manager, tc_manager = self.run_tunnel_managers()

        # create alias for ts_manager and tc_manager using sender or receiver
        if self.sender_side == self.server_side:
            send_manager = ts_manager
            recv_manager = tc_manager
        else:
            send_manager = tc_manager
            recv_manager = ts_manager

        # run every flow
        second_cmds = []
        for tun_id in xrange(1, self.flows + 1):
            # run tunnel server for tunnel tun_id
            cmd_to_run_tc = self.run_tunnel_server(tun_id, ts_manager)

            # run tunnel client for tunnel tun_id
            self.run_tunnel_client(tun_id, tc_manager, cmd_to_run_tc)

            tc_pri_ip = cmd_to_run_tc[3]  # tunnel client private IP
            ts_pri_ip = cmd_to_run_tc[4]  # tunnel server private IP

            if self.sender_side == self.server_side:
                send_pri_ip = ts_pri_ip
                recv_pri_ip = tc_pri_ip
            else:
                send_pri_ip = tc_pri_ip
                recv_pri_ip = ts_pri_ip

            # run the side that runs first and get cmd to run the other side
            second_cmd = self.run_first_side(
                tun_id, send_manager, recv_manager, send_pri_ip, recv_pri_ip)
            second_cmds.append(second_cmd)

        # run the side that runs second
        self.run_second_side(send_manager, recv_manager, second_cmds)

        # stop all the running flows and quit tunnel managers
        ts_manager.stdin.write('halt\n')
        tc_manager.stdin.write('halt\n')

        # process tunnel logs
        self.process_tunnel_logs()

    def process_tunnel_logs(self):
        datalink_tun_logs = []
        acklink_tun_logs = []

        for i in xrange(self.flows):
            tun_id = i + 1
            if self.mode == 'remote':
                # download logs from remote side
                cmd = 'scp -C %s:' % self.r['host_addr']
                cmd += '%(log)s %(log)s'

                if self.sender_side == 'remote':
                    check_call(cmd % {'log': self.acklink_ingress_logs[i]},
                               shell=True)
                    check_call(cmd % {'log': self.datalink_egress_logs[i]},
                               shell=True)
                else:
                    check_call(cmd % {'log': self.datalink_ingress_logs[i]},
                               shell=True)
                    check_call(cmd % {'log': self.acklink_egress_logs[i]},
                               shell=True)

            uid = uuid.uuid4()
            datalink_tun_log = path.join(
                TMPDIR, '%s_flow%s_uid%s.log.merged'
                % (self.datalink_name, tun_id, uid))
            acklink_tun_log = path.join(
                TMPDIR, '%s_flow%s_uid%s.log.merged'
                % (self.acklink_name, tun_id, uid))

            cmd = ['merge-tunnel-logs', 'single',
                   '-i', self.datalink_ingress_logs[i],
                   '-e', self.datalink_egress_logs[i],
                   '-o', datalink_tun_log]
            check_call(cmd)

            cmd = ['merge-tunnel-logs', 'single',
                   '-i', self.acklink_ingress_logs[i],
                   '-e', self.acklink_egress_logs[i],
                   '-o', acklink_tun_log]
            check_call(cmd)

            datalink_tun_logs.append(datalink_tun_log)
            acklink_tun_logs.append(acklink_tun_log)

        cmd = ['merge-tunnel-logs', 'multiple', '-o', self.datalink_log]
        if self.mode == 'local':
            cmd += ['--link-log', self.mm_datalink_log]
        cmd += datalink_tun_logs
        check_call(cmd)

        cmd = ['merge-tunnel-logs', 'multiple', '-o', self.acklink_log]
        if self.mode == 'local':
            cmd += ['--link-log', self.mm_acklink_log]
        cmd += acklink_tun_logs
        check_call(cmd)

    def run_congestion_control(self):
        if self.flows > 0:
            self.run_with_tunnel()
        else:
            # test without pantheon tunnel when self.flows = 0
            self.run_without_tunnel()

    def record_time_stats(self):
        stats_log = path.join(
            self.test_dir, '%s_stats_run%s.log' % (self.cc, self.run_id))
        stats = open(stats_log, 'w')

        # save start time and end time of test
        test_run_duration = (
            'Start at: %s\nEnd at: %s\n' %
            (self.test_start_time, self.test_end_time))
        sys.stderr.write(test_run_duration)
        stats.write(test_run_duration)

        if self.mode == 'remote' and self.worst_clock_offset is not None:
            offset_info = ('Worst absolute clock offset: %s ms\n'
                           % self.worst_clock_offset)
            sys.stderr.write(offset_info)
            stats.write(offset_info)

        stats.close()

    # run congestion control test
    def run(self):
        # initialize colored output
        colorama.init()

        sys.stderr.write(
            Back.BLUE + 'Testing %s...\n' % self.cc + Style.RESET_ALL)

        # setup before running tests
        self.setup()

        # run receiver and sender
        self.run_congestion_control()

        # write runtimes and clock offsets to file
        self.record_time_stats()

        sys.stderr.write(
            Back.GREEN + 'Done testing %s\n' % self.cc + Style.RESET_ALL)


def main():
    args = parse_arguments(path.basename(__file__))

    if args.all:
        cc_schemes = parse_config().keys()
    elif args.schemes is not None:
        cc_schemes = args.schemes.split()

    for run_id in xrange(1, args.run_times + 1):
        for cc in cc_schemes:
            Test(args, run_id, cc).run()


if __name__ == '__main__':
    main()
