#!/usr/bin/python

import sys
import time
import datetime
import pickle
import json
import syslog

import boto3
import boto
from boto.ec2.cloudwatch import CloudWatchConnection


class WatchData:
    dry = False
    low_limit = 70
    low_counter_limit = 0
    high_counter_limit = 0
    high_limit = 90
    high_urgent = 95
    stats_period = 60
    history_size = 0

    def __init__(self, name):
        self.name = name
        self.datafile = "/tmp/watchdata-{}.p".format(self.name)
        self.instances = 0
        self.new_desired = 0
        self.desired = 0
        self.instances_info = None
        self.previous_instances = 0
        self.action = ""
        self.action_ts = 0
        self.changed_ts = 0
        self.total_load = 0
        self.avg_load = 0
        self.max_load = 0
        self.up_ts = 0
        self.down_ts = 0
        self.low_counter = 0  # count the consecutive times a low conditions has been observed
        self.high_counter = 0  # count the consecutive times a high conditions has been observed
        self.max_loaded = None
        self.loads = {}
        self.measures = {}
        self.emergency = False
        self.history = None
        self.trend = 0
        self.exponential_average = 0
        self.ts = 0

    def __getstate__(self):
        """ Don't store these objets """
        d = self.__dict__.copy()
        del d['ec2']
        del d['cw']
        del d['autoscale']
        del d['group']
        del d['instances_info']
        return d

    def connect(self):
        self.ec2 = boto.connect_ec2()
        self.cw = CloudWatchConnection()
        self.autoscale = boto3.client('autoscaling')
        g = self.autoscale.describe_auto_scaling_groups(AutoScalingGroupNames=[self.name], MaxRecords=100)
        
        if len(g) < 1:
          print("No instances found for AutoScaling group {}".format(self.name))
          sys.exit(1)
        #self.group = self.autoscale.get_all_groups(names=[self.name])[0]
        self.group = g['AutoScalingGroups'][0]
        self.instances = len(self.group['Instances']) # TODO: Check "InService"
        self.desired = self.group['DesiredCapacity']
        self.max_size = self.group['MaxSize']
        self.min_size = self.group['MinSize']
        self.name = self.name
        self.ts = int(time.time())

    def get_instances_info(self):
        ids = [i['InstanceId'] for i in self.group['Instances']]
        self.instances_info = self.ec2.get_only_instances(instance_ids=ids)

    def get_CPU_loads(self):
        """ Read instances load and store in data """
        measures = 0
        for instance in [i['InstanceId'] for i in self.group['Instances']]:
            load = self.get_instance_CPU_load(instance)
            if load is None:
                continue
            measures += 1
            self.total_load += load
            self.loads[instance] = load
            if load > self.max_load:
                self.max_load = load
                self.max_loaded = instance

        if measures > 0:
            self.avg_load = self.total_load / measures

    def get_instance_CPU_load(self, instance):
        end = datetime.datetime.now()
        start = end - datetime.timedelta(seconds=int(self.stats_period * 3))

        m = self.cw.get_metric_statistics(
            self.stats_period, start, end, "CPUUtilization", "AWS/EC2",
            ["Average"], {"InstanceId": instance})
        if len(m) > 0:
            measures = self.measures[instance] = len(m)
            ordered = sorted(m, key=lambda x: x['Timestamp'])
            return ordered[-1]['Average']  # Return last measure

        return None

    def from_file(self):
        try:
            data = pickle.load(open(self.datafile, "rb"))
        except:
            data = WatchData('_previous')

        return data

    def store(self):
        if self.history_size > 0:
            if not self.history: self.history = []
            self.history.append([
                int(time.time()), len(self.group['Instances']),
                int(round(self.total_load)), int(round(self.avg_load))
            ])
            self.history = self.history[-self.history_size:]

        pickle.dump(self, open(self.datafile, "wb"))

    def check_too_low(self):
        for instance, load in self.loads.iteritems():
            if load is not None and self.measures[
                    instance] > 1 and self.instances > 1 and load < self.avg_load * 0.2 and load < 4:
                self.emergency = True
                self.check_avg_low() # Check if the desired instanes can be decreased
                self.action = "Warning: terminated instance with low load (%s %5.2f%%) " % (instance, load)
                self.kill_instance(instance)
                return True
        return self.emergency

    def check_too_high(self):
        for instance, load in self.loads.iteritems():
            if load is None or self.measures[instance] <= 1:
                continue
            if self.instances > 2 and load > self.avg_load * 1.4:  # kill if it consumes more than 40% of the average
                self.emergency = True
                self.action = "Emergency: kill bad instance with high load (%s %5.2f%%) " % (instance, load)
                self.kill_instance(instance)
                if self.avg_load < self.high_limit:
                    self.set_desired(self.instances - 1)
                return True

            if load > self.high_urgent:
                self.emergency = True
                self.action = "Emergency: high load in one instance (%s %5.2f%%) " % (instance, load)
                self.action += " increasing instances to %d" % (self.instances + 1, )
                self.set_desired(self.instances + 1)
                return True

        return self.emergency

    def check_avg_high(self):
        if self.instances >= self.max_size:
            self.high_counter = 0
            return False

        threshold = self.high_limit
        if self.instances == 1:
            threshold = threshold * 0.90  # Increase faster if there is just one instance

        if self.avg_load > threshold:
            self.high_counter += 1
            if self.high_counter > self.high_counter_limit:
                self.high_counter = 0
                self.action = "WARN, high load (%5.2f/%5.2f): %d -> %d " % (
                    self.avg_load, threshold, self.instances,
                    self.instances + 1)
                self.set_desired(self.instances + 1)
                return True

        else:
            self.high_counter = 0

    def check_avg_low(self):
        if self.instances <= self.min_size:
            self.low_counter = 0
            return False

        threshold = self.low_limit
        if self.instances < 3:
            threshold = threshold * 0.95

        if self.total_load / (self.instances - 1) < threshold:
            self.low_counter += 1
            if self.low_counter > self.low_counter_limit:
                self.low_counter = 0
                self.action = "low load (%5.2f/%5.2f): %d -> %d " % (
                    self.avg_load, threshold, self.instances,
                    self.instances - 1)
                self.set_desired(self.instances - 1)
        else:
            self.low_counter = 0

    def kill_instance(self, id):
        if self.action:
            print(self.action)
        print("Kill instance", id)
        syslog.syslog(syslog.LOG_INFO,
                      "ec2_watch kill_instance: %s instances: %d (%s)" %
                      (id, self.instances, self.action))
        if self.dry:
            return
        self.ec2.terminate_instances(instance_ids=[id])
        self.action_ts = time.time()

    def set_desired(self, desired):
        if self.action:
            print(self.action)
        print("Setting instances from %d to %d" % (self.instances, desired))
        syslog.syslog(syslog.LOG_INFO, "ec2_watch set_desired: %d -> %d (%s)" %
                      (self.instances, desired, self.action))
        if self.dry:
            return
        if desired >= self.min_size and desired <= self.max_size:
            self.group.set_desired_capacity(AutoScalingGroupName=self.name, DesiredCapacity=desired)
        self.action_ts = time.time()
        self.new_desired = desired
