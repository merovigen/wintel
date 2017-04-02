import gammu
import time
from peewee import SqliteDatabase, Model, IntegerField, TextField, IntegrityError
import os
import flock

# TODO test locking

db = SqliteDatabase('/var/db/sms.db')


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
    number = IntegerField(unique=True)

    class Meta:
        database = db


def init(config_path=None):
    state_machine = gammu.StateMachine()
    state_machine.ReadConfig(Filename=config_path)
    state_machine.Init()
    return state_machine


def modem_show():
    pass


def modem_add(imsi, number, description):
    pass


def modem_delete(imsi):
    pass


def __system_scan():
    # Scans /dev for ttyUSB devices and returns absolute paths list for config generation
    # eg ['/dev/ttyUSB0', /dev/'ttyUSB2']
    return ['/dev/ttyUSB0']


def __generate_gammu_config(modem_path):
    # generate temp config file for each modem and returns system absolute path, eg /var/conf/$id.conf
    config_path = '/var/tmp/gammu.conf'
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


def get_all_sms(state_machine):
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
        message_number = message_number - len(cursms)
        sms_list.append(cursms)
    return sms_list


def main():
    with open('/tmp/wintel.lock', 'w') as f:
        with flock.Flock(f, flock.LOCK_EX):
            try:
                # First, we must do a system scan and find all modems
                for modem_path in __system_scan():
                    config_path = __generate_gammu_config(modem_path)
                    state_machine = init(config_path)
                    imsi = int(state_machine.GetSIMIMSI())
                    for sms in get_all_sms(state_machine):
                        sms = sms[0]
                        try:
                            Message.create(imsi=imsi,
                                           timestamp=int(time.mktime(sms['DateTime'].timetuple())),
                                           sender=sms['Number'].encode('utf-8'),
                                           content=sms['Text'].encode('utf-8')
                                           )
                        except IntegrityError:
                            print "Tried to add duplicate message to database with timestamp {}".format(
                                int(time.mktime(sms['DateTime'].timetuple()))
                            )
                            break
                        # delete message from modem
                        else:
                            pass
                            # state_machine.DeleteSMS(0, sms['Location'])
                    # delete temp config file
                    os.remove(config_path)
            except IOError:
                print 'Lock exists, previous run might be working, exiting now.'
                return



if __name__ == '__main__':
    db.connect()
    main()
