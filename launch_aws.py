#!/usr/bin/env python
import boto3
import botocore.exceptions
from paramiko import SSHClient, AutoAddPolicy
from scp import SCPClient
import time
import os
import socket
import requests


def signal_term_handler(func, signal, frame):
    choice = raw_input("Are you sure you want to exit? (y or n): ")
    if choice.lower() == 'y':
        print('tearing down')
        import sys
        func()
        sys.exit(0)
    pass


class Launcher(object):
    def __init__(self):
        self.timestamp = 'openquake-' + str(int(time.time()))
        self.ec2 = boto3.resource('ec2')
        self.key = None
        self.security_group = None
        self.instance = None
        self.ip_address = None

    def setup(self):
        # SSH keypair
        print('- creating SSH keypair')
        self.key = self.ec2.create_key_pair(KeyName=self.timestamp)
        with open(self.timestamp + '.pem', 'w') as f:
            f.write(self.key.key_material)
        os.chmod(self.timestamp + '.pem', 0o600)

        # security group
        print('- creating security group')
        self.security_group = self.ec2.create_security_group(GroupName=self.timestamp,
                                                             Description='openquake')

        # allow port 22 and 8080
        self.security_group.authorize_ingress(IpPermissions=[{'IpProtocol': 'tcp',
                                                              'FromPort': 22,
                                                              'ToPort': 22,
                                                              'IpRanges': [{'CidrIp': '0.0.0.0/0'}]},
                                                             ])
        self.security_group.authorize_ingress(IpPermissions=[{'IpProtocol': 'tcp',
                                                              'FromPort': 8080,
                                                              'ToPort': 8080,
                                                              'IpRanges': [{'CidrIp': '0.0.0.0/0'}]}
                                                             ])

        # launch master node
        print('- launching instance')
        instance = self.ec2.create_instances(ImageId='ami-6c14310f',
                                             InstanceType='t2.micro',
                                             InstanceInitiatedShutdownBehavior='terminate',
                                             MinCount=1,
                                             MaxCount=1,
                                             SecurityGroupIds=[self.timestamp],
                                             KeyName=self.timestamp)

        # TODO create tag for instance

        self.instance = instance[0]
        self.instance.wait_until_running()
        self.ip_address = self.ec2.Instance(self.instance.id).public_ip_address
        print('- launched: ' + self.ip_address)

    def deploy(self):
        # poll SSH
        print('- polling SSH until successful connection')
        while True:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(1)
            try:
                s.connect((self.ip_address, 22))
                break
            except:
                pass

            s.close()
            time.sleep(1)

        # SSH to instance
        print('- connecting to instance via SSH (ssh -i ' + self.timestamp + '.pem ubuntu@' + self.ip_address + ')')
        ssh = SSHClient()
        ssh.set_missing_host_key_policy(AutoAddPolicy())
        ssh.connect(hostname=self.ip_address,
                    port=22,
                    username='ubuntu',
                    key_filename=self.timestamp + '.pem')

        with SCPClient(ssh.get_transport()) as scp:
            # copy files to instance
            print('- copying files to instance')
            scp.put(files=['master_script.sh',
                           'webserver.py',
                           'openquake'],
                    remote_path='/tmp/',
                    recursive=True)

            # run shell script on instance
            print('- executing script on instance')
            stdin, stdout, stdrr = ssh.exec_command('cd /tmp; \
                                                     chmod +x master_script.sh; \
                                                     nohup ./master_script.sh  > aws_log.log 2>&1 &')

            # poll web service for logs, until 'done' received
            print('- polling instance for logs (run \'tail -f aws_log.log\' to get running output')
            log_count = 0
            while True:
                time.sleep(1)
                try:
                    r = requests.get('http://' + self.ip_address + ':8080')
                    json = r.json()
                    new_log_count = len(json['logs'])
                    if new_log_count > log_count:
                        for log in json['logs'][log_count:]:
                            print(log['time'] + ': ' + log['msg'])
                        log_count += new_log_count - log_count
                    if json['done']:
                        break
                except:
                    pass

                # download aws_log.log
                try:
                    scp.get(remote_path='/tmp/aws_log.log',
                            local_path='.')
                except:
                    pass

            print('downloading results')
            try:
                scp.get(remote_path='/home/ubuntu/oqdata',
                        local_path='oqdata-' + self.timestamp,
                        recursive=True)
            except Exception as e:
                print('Error - could not download results: ' + str(e))
                print('Check aws_log.log for logs of EC2 instance.')
                # print('You could also connect to instance using (in another terminal session, while keeping this '
                #       'program running): ssh -i ' + self.timestamp + '.pem' + ' ubuntu@' + self.ip_address)
                # raw_input('\n\n[Press any key to teardown]')

    def teardown(self):
        print('- terminating instance')
        self.instance.terminate()
        self.instance.wait_until_terminated()  # must terminate instance first, then security group

        print('- deleting security group')
        self.security_group.delete()

        print('- deleting SSH key')
        self.key.delete()
        os.remove(self.timestamp + '.pem')


if __name__ == '__main__':
    launcher = Launcher()
    # capture kill and Ctrl+C to give script chance to tear down
    import signal
    from functools import partial
    signal.signal(signal.SIGTERM, partial(signal_term_handler, launcher.teardown))
    signal.signal(signal.SIGINT, partial(signal_term_handler, launcher.teardown))

    try:
        print('setting up')
        launcher.setup()
        print('deploying')
        launcher.deploy()

    except Exception as e:
        print("Error - could not run: " + str(e))
    finally:
        print('tearing down finally')
        launcher.teardown()
