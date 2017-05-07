import gammu
import time
from peewee import SqliteDatabase, Model, IntegerField, TextField, IntegrityError, DoesNotExist
from collections import OrderedDict
import os
import flock
import click
import re
import logging
import json
import datetime
import glob
import subprocess

from flask import Flask
app = Flask(__name__)

# TODO test locking

with open('config.json') as config_file:
    config = json.load(config_file)

db = SqliteDatabase(config['db_file_path'].encode('utf8'))
logging.basicConfig(format='%(asctime)s %(levelname)-8s %(message)s',
                    level=logging.INFO,
                    filename=config['log_file_path'].encode('utf8'))
state_machine_list = []


class Message(Model):
    timestamp = IntegerField(unique=True, primary_key=True)
    imsi = IntegerField()
    sender = TextField()
    content = TextField()

    class Meta:
        database = db


class Number(Model):
    description = TextField()
    imsi = IntegerField(unique=True, primary_key=True)
    number = IntegerField()
    cid = TextField()

    class Meta:
        database = db


class UnSortedGroup(click.Group):
    def __init__(self, *args, **kwargs):
        super(UnSortedGroup, self).__init__(*args, **kwargs)
        self.commands = OrderedDict()

    def list_commands(self, ctx):
        return self.commands


def group(name=None, **attrs):
    attrs.setdefault('cls', UnSortedGroup)
    return click.command(name, **attrs)


@group()
def cli():
    pass


def init(config_path=None):
    try:
        state_machine = gammu.StateMachine()
        state_machine.ReadConfig(Filename=config_path)
        state_machine.Init()
    except gammu.ERR_TIMEOUT:
        return
    else:
        return state_machine


@cli.command()
@click.option('--imsi', type=click.STRING, required=True, help="Modem IMSI", show_default=True)
def sim_show(imsi):
    try:
        modem = Number.get(imsi=imsi)
    except DoesNotExist:
        logging.error('No modem with IMSI {}'.format(imsi))
        print 'Error: no modem with IMSI {}'.format(imsi)
    else:
        print 'IMSI: {}\n' \
              'Number: +7 {}\n' \
              'Description: {}\n' \
              'Last CID: {}'.format(modem.imsi, modem.number, modem.description, modem.cid)


@cli.command()
@click.option('--imsi', type=click.STRING, required=True, help="Modem IMSI", show_default=True)
@click.option('--number', type=click.STRING, required=True, help="Modem number", show_default=True)
@click.option('--description', type=click.STRING, required=False, help="Description", show_default=True)
def sim_add(imsi, number, description):
    try:
        Number.get(imsi=imsi)
    except DoesNotExist:
        Number.create(imsi=imsi, number=number, description=description, cid=0)
        logging.info('Added modem, IMSI: {}, number: {}, description: {}'.format(imsi, number, description))
    else:
        logging.error('Error: modem with this IMSI already exist')
        print 'Modem with this IMSI already exist'


@cli.command()
@click.option('--imsi', type=click.STRING, required=True, help="Modem IMSI", show_default=True)
@click.option('--number', type=click.STRING, required=False, help="Modem number", show_default=True)
@click.option('--description', type=click.STRING, required=False, help="Description", show_default=True)
def sim_modify(imsi, number, description):
    try:
        number_object = Number.get(imsi=imsi)
    except DoesNotExist:
        logging.error('No modem with IMSI {}'.format(imsi))
        print 'Error: no modem with IMSI {}'.format(imsi)
    else:
        if number:
            number_object.number = number
        if description:
            number_object.description = description
        number_object.save()
        logging.info('Modified modem, IMSI: {}, number: {}, description: {}'.format(imsi, number, description))


@cli.command()
@click.option('--imsi', type=click.STRING, required=True, help="User login", show_default=True)
def sim_delete(imsi):
    try:
        modem = Number.get(imsi=imsi)
    except DoesNotExist:
        logging.error('No modem with IMSI {}'.format(imsi))
        print 'Error: no modem with IMSI {}'.format(imsi)
    else:
        modem.delete_instance()
        logging.info('Deleted modem, IMSI: {}'.format(imsi))


def __system_scan():
    # Scans /dev for ttyUSB devices and returns absolute paths list for config generation
    # eg ['/dev/ttyUSB0', /dev/'ttyUSB2']
    modem_list = []
    for dev in os.listdir('/dev'):
        if 'ttyUSB' in dev and int(re.search(r'ttyUSB(.*)', dev).group(1)) % 2 == 0:
            modem_list.append('/dev/'+dev)
    return modem_list
    # return ['/dev/ttyUSB0', '/dev/ttyUSB2']


def __generate_gammu_config(modem_path):
    # generate temp config file for each modem and returns system absolute path, eg /var/conf/$id.conf
    gammu_config_path = '{}/gammu_{}.conf'.format(config['tmp_config_dir'].encode('utf8'),
                                            re.search('/dev/(.*)', modem_path).group(1))
    with open(gammu_config_path, 'w') as f:
        f.write("""[gammu]
port = {}
connection = at115200
synchronizetime = yes
logformat = errorsdate
gammucoding = utf8
""".format(modem_path))
    return gammu_config_path


def __logger(message):
    log_path = '/var/log/wintel.log'
    mode = 'a'
    if not os.path.isfile(log_path):
        mode = 'w'
    with open(log_path, mode) as f:
        # TODO add datetime to message
        f.write(message)
    pass


def disable_modem(state_machine):
    # udevadm returns string like this:
    # '/devices/pci0000:00/0000:00:1d.0/usb2/2-1/2-1.2/2-1.2:1.0/ttyUSB0/tty/ttyUSB0\n'
    # we need this part:                         ^^^^^
    usb_path = subprocess.check_output('udevadm info -q path -n {}'.format(state_machine.GetConfig()['Device']),
                                       shell=True).rsplit('/', 5)[1]
    # disable device
    subprocess.call('echo 1 > /sys/bus/usb/drivers/usb/{}/remove'.format(usb_path), shell=True)


def read_sms_by_modem(state_machine):
    status = state_machine.GetSMSStatus()

    message_number = status['SIMUsed'] + status['PhoneUsed'] + status['TemplatesUsed']

    sms_list = []
    start = True

    while message_number > 0:
        if start:
            message = state_machine.GetNextSMS(Start=True, Folder=0)
            start = False
        else:
            message = state_machine.GetNextSMS(Location=message[0]['Location'], Folder=0)
        message_number -= len(message)
        sms_list.append(message)
    return sms_list


def read_sms():
    with open('/tmp/wintel.lock', 'w') as lock:
        with flock.Flock(lock, flock.LOCK_EX):
            try:
                for state_machine in state_machine_list:
                    imsi = int(state_machine.GetSIMIMSI())
                    for message in read_sms_by_modem(state_machine):
                        message = message[0]
                        try:
                            Message.create(imsi=imsi,
                                           timestamp=int(time.mktime(message['DateTime'].timetuple())),
                                           sender=message['Number'].encode('utf-8'),
                                           content=message['Text'].encode('utf-8')
                                           )
                            logging.info('Added message, IMSI: {}, timestamp: {}, sender: {}, content: {}'.format(
                                imsi,
                                message['DateTime'],
                                message['Number'].encode('utf-8'),
                                message['Text'].encode('utf-8')
                            ))
                        except IntegrityError:
                            logging.error("Tried to add duplicate message to database with timestamp {}".format(
                                message['DateTime']))
                            print "Tried to add duplicate message to database with timestamp {}".format(
                                message['DateTime'])
                        # delete message from modem
                        else:
                            # print message
                            if config['delete_messages']:
                                state_machine.DeleteSMS(0, message['Location'])
                    # delete temp config file
                    # os.remove(config_path)
            except IOError:
                logging.warning('Lock exists, previous run might be working, exiting.')
                print 'Lock exists, previous run might be working, exiting.'


@app.route('/cid')
def update_cid():
    for state_machine in state_machine_list:
        imsi = int(state_machine.GetSIMIMSI())
        network_info = state_machine.GetNetworkInfo()
        try:
            modem = Number.get(imsi=imsi)
        except DoesNotExist:
            logging.warning('Modem with IMSI {} is not in database, adding to database!'.format(imsi))
            Number.create(imsi=imsi, number=0, description='', cid=network_info['CID'])
        else:
            last_cid = modem.cid if modem.cid else '0'
            if modem.cid != network_info['CID']:
                modem.cid = network_info['CID']
                modem.save()
                logging.info('CID changed to {} from {} for modem with IMSI {}'.format(
                    network_info['CID'],
                    last_cid,
                    imsi))
                if config['paranoid_mode']:
                    disable_modem(state_machine)
                    state_machine_list.remove(state_machine)
                    logging.warning('Paranoid mode on, disabled modem with IMSI {}!'.format(imsi))
    # flask require callable object to be returned
    return ''


@cli.command()
def web():
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR)
    #

    for modem_path in __system_scan():
        config_path = __generate_gammu_config(modem_path)
        state_machine_list.append(init(config_path))
    logging.info('Application started')
    app.run(host=config['web_address'])
    logging.info('Application stopped')
    # post cleanup, delete all config
    for gammu_file in glob.glob('{}/*.conf'.format(config['tmp_config_dir'])):
        os.remove(gammu_file)


@app.after_request
def treat_as_plain_text(response):
    response.headers["content-type"] = "text/plain"
    return response


@app.route('/sms')
def test():
    read_sms()
    messages = ''
    for message in Message.select(Message, Number).join(Number, on=(Message.imsi == Number.imsi).alias('num')).order_by(Message.timestamp.desc()):
        messages += '{} [{}] +7{} | \n'.format(message.sender, datetime.datetime.fromtimestamp(message.timestamp),
                                               message.num.number,
                                               message.num.description)
        messages += '{} \n'.format(message.content.encode('utf-8'))
        messages += '\n'
    return messages


def main():
    cli()


if __name__ == '__main__':
    db.connect()
    db.create_tables([Message, Number], safe=True)
    main()
    db.close()
