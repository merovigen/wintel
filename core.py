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

from flask import Flask
app = Flask(__name__)

# TODO test locking

with open('config.json') as f:
    config = json.load(f)

db = SqliteDatabase(config['db_file_path'].encode('utf8'))
logging.basicConfig(format='%(asctime)s %(levelname)-8s %(message)s',
                    level=logging.INFO,
                    filename=config['log_file_path'].encode('utf8'))


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
    # return ['/dev/ttyUSB0']


def __generate_gammu_config(modem_path):
    # generate temp config file for each modem and returns system absolute path, eg /var/conf/$id.conf
    config_path = '{}/gammu.conf'.format(config['tmp_config_dir'].encode('utf8'))
    with open(config_path, 'w') as f:
        f.write("""[gammu]
port = {}
connection = at115200
synchronizetime = yes
logformat = errorsdate
gammucoding = utf8
""".format(modem_path))
    return config_path


def __logger(message):
    log_path = '/var/log/wintel.log'
    mode = 'a'
    if not os.path.isfile(log_path):
        mode = 'w'
    with open(log_path, mode) as f:
        # TODO add datetime to message
        f.write(message)
    pass


def read_sms_by_modem(state_machine):
    status = state_machine.GetSMSStatus()

    message_number = status['SIMUsed'] + status['PhoneUsed'] + status['TemplatesUsed']

    sms_list = []
    start = True

    while message_number > 0:
        if start:
            cursms = state_machine.GetNextSMS(Start=True, Folder=0)
            start = False
        else:
            cursms = state_machine.GetNextSMS(Location=cursms[0]['Location'], Folder=0)
        message_number -= len(cursms)
        sms_list.append(cursms)
    return sms_list


def read_sms():
    with open('/tmp/wintel.lock', 'w') as lock:
        with flock.Flock(lock, flock.LOCK_EX):
            try:
                # First, we must do a system scan and find all modems
                for modem_path in __system_scan():
                    config_path = __generate_gammu_config(modem_path)
                    state_machine = init(config_path)
                    if state_machine is None:
                        logging.error('Unable to init modem on {}'.format(modem_path))
                        break
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
                            print message
                            if config['delete_messages']:
                                state_machine.DeleteSMS(0, message['Location'])
                    # delete temp config file
                    os.remove(config_path)
            except IOError:
                logging.warning('Lock exists, previous run might be working, exiting.')
                print 'Lock exists, previous run might be working, exiting.'


@cli.command()
def update_cid():
    for modem_path in __system_scan():
        config_path = __generate_gammu_config(modem_path)
        state_machine = init(config_path)
        if state_machine is None:
            logging.error('Unable to init modem on {}'.format(modem_path))
            break
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


@cli.command()
def web():
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR)
    app.run(host=config['web_address'])


@app.after_request
def treat_as_plain_text(response):
    response.headers["content-type"] = "text/plain"
    return response


@app.route('/')
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
