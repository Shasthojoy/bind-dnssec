#!/usr/bin/env python
# -*- coding: utf8 -*-

import os
import sys
import datetime
import subprocess
import argparse
from functools import total_ordering


BASE = "/etc/bind/keys"
# Interval entre 2 opérations sur les clefs dns.
# Par exemple si vous avec la clef1 d'utilisé,
# clef2 est publié INTERVAL avant la désactivation de clef1.
# clef1 est désactivé quand clef2 est activé,
# clef2 est supprimé INTRERVAL après sa désactivation.
# INTERVAL DOIT être supérieur aux plus long TTL que les enregistrement DS peuvent avoir.
# INTERVAL DOIT egalement être supérieur a l'intervale de signature de bind (défaut de 22.5 jours)
# Cela dépent essentiellement de la configuration de la zone parente et vous n'avez pas forcement
# de controle dessus.
INTERVAL = datetime.timedelta(days=23)
# Durée au bout de laquelle une ZSK est remplacé par une nouvelle ZSK.
# La génération des ZSK et leur activation/désactivation/suppression est géré
# automatiquement tant que routine.py -c est appelé au moins une fois par
# jour.
ZSK_VALIDITY = datetime.timedelta(days=30)  # ~1 mois
# Temps au bout duquelle une nouvelle KSK est généré et publié pour la zone
# (et activé après INTERVAL). L'ancienne clef n'est retiré que INTERVAL après que la nouvelle
# clef a été routine.py --ds-seen. Cela demande en général une opération
# manuelle avec le registrar (publier le DS de la nouvelle clef dans la zone parente)
# et routine.py -c affiche un message tant que cela n'a pas été fait.
KSK_VALIDITY = datetime.timedelta(days=366)  # ~1 an


def get_zones(zone_names=None):
    l = []
    if zone_names is None:
        for f in os.listdir(BASE):
            if os.path.isdir(os.path.join(BASE, f)) and not f.startswith('.'):
                l.append(Zone(f))
    else:
        for name in zone_names:
            l.append(Zone(name))
    return l


def settime(path, flag, date):
    cmd = [
        "/usr/sbin/dnssec-settime",
        "-i", str(int(INTERVAL.total_seconds())),
        "-%s" % flag, date, path
    ]
    p = subprocess.Popen(cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE)
    err = p.communicate()[1]
    if p.returncode != 0:
        raise ValueError("err %s: %s" % (p.returncode, err))
    if err:
        sys.stderr.write("%s\n" % err)


def bind_chown(path):
    os.chown(path, 104, -1)
    for root, dirs, files in os.walk(path):
        for momo in dirs:
            os.chown(os.path.join(root, momo), 104, -1)
        for momo in files:
            os.chown(os.path.join(root, momo), 104, -1)


def bind_reload():
    cmd = ["/usr/sbin/rndc", "reload"]
    p = subprocess.Popen(cmd)
    p.wait()


def nsec3(zone, salt="-"):
    cmd = ["rndc", "signing", "-nsec3param", "1", "0", "10", salt, zone]
    sys.stdout.write("Enabling nsec3 for zone %s: " % zone)
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    out = p.communicate()[0]
    sys.stdout.write(out)
    p.wait()


class Zone(object):
    ZSK = None
    KSK = None
    _path = None
    name = None

    def __str__(self):
        return self.name

    def __unicode__(self):
        return self.name.decode("utf-8")

    def __repr__(self):
        return "Zone %s" % self.name

    @classmethod
    def create(cls, name):
        path = os.path.join(BASE, name)
        if os.path.isdir(path):
            raise ValueError("%s existe" % path)
        os.mkdir(path)
        bind_chown(path)
        return cls(name)

    def do_zsk(self):
        for zsk in self.ZSK:
            if zsk.is_activate:
                zsk.inactive = zsk.activate + ZSK_VALIDITY
                zsk.delete = zsk.inactive + INTERVAL
                last_activate_zsk = zsk
        now = datetime.datetime.utcnow()
        if zsk.is_activate:
            zsk.inactive = max(zsk.inactive, now + INTERVAL)
            zsk.delete = zsk.inactive + INTERVAL
            zsk.gen_successor()
            bind_reload()
        else:
            zsk.activate = last_activate_zsk.inactive

    def do_ksk(self):
        ksk = self.KSK[-1]
        if ksk.need_renew:
            now = datetime.datetime.utcnow()
            new_ksk = Key.create("KSK", self.name)
            new_ksk.publish = now
            # do not activate the new key until ds-seen
            new_ksk.activate = None
            bind_reload()
        active_ksk = [key for key in self.KSK if key.is_publish and key.delete is None]
        if len(active_ksk) >= 2:
            sys.stderr.write(
                (
                    "New KSK needs DS seen and/or old KSK needs "
                    "inactivate/remove for zone %s\n"
                ) % self.name
            )

    def ds_seen(self, keyid):
        old_ksks = []
        for ksk in self.KSK:
            if ksk.keyid == keyid:
                seen_ksk = ksk
                break
            old_ksks.append(ksk)
        else:
            sys.stderr.write("Key not found\n")
            return
        print "Key %s found" % keyid
        now = datetime.datetime.utcnow()
        if seen_ksk.activate is None:
            seen_ksk.activate = (now + INTERVAL)
        for ksk in old_ksks:
            print " * program key %s removal" % ksk.keyid
            # set inactive in at least INTERVAL
            ksk.inactive = seen_ksk.activate
            # delete INTERVAL after being inactive
            ksk.delete = ksk.inactive + INTERVAL
        bind_reload()

    def remove_deleted(self):
        deleted_path = os.path.join(self._path, "deleted")
        try:
            os.mkdir(deleted_path)
        except OSError as error:
            if error.errno != 17:  # File exists
                raise
        now = datetime.datetime.utcnow()
        for key in self.ZSK + self.KSK:
            if key.delete and (key.delete + INTERVAL) <= now:
                for path in [key._path, key._path_private]:
                    basename = os.path.basename(path)
                    new_path = os.path.join(deleted_path, basename)
                    os.rename(path, new_path)

    def ds(self):
        for ksk in self.KSK:
            cmd = ["/usr/sbin/dnssec-dsfromkey", ksk._path]
            p = subprocess.Popen(cmd)
            p.wait()

    def key(self):
        for ksk in self.KSK:
            print ksk

    def __init__(self, name):
        path = os.path.join(BASE, name)
        if not os.path.isdir(path):
            raise ValueError("%s n'est pas un dossier" % path)
        self.name = name
        self._path = path
        self.ZSK = []
        self.KSK = []
        for file in os.listdir(path):
            file_path = os.path.join(path, file)
            if os.path.isfile(file_path) and file_path.endswith(".private"):
                try:
                    key = Key(file_path)
                    if key.type == "ZSK":
                        self.ZSK.append(key)
                    elif key.type == "KSK":
                        self.KSK.append(key)
                    else:
                        raise RuntimeError("impossible")
                except ValueError as error:
                    sys.stderr.write("%s\n" % error)
        self.ZSK.sort()
        self.KSK.sort()
        if not self.ZSK:
            self.ZSK.append(Key.create("ZSK", name))
        if not self.KSK:
            self.KSK.append(Key.create("KSK", name))


@total_ordering
class Key(object):
    _created = None
    _publish = None
    _activate = None
    _inactive = None
    _delete = None
    _data = None
    _path = None
    type = None
    keyid = None
    flag = None
    zone_name = None

    def __str__(self):
        return self._data

    def __repr__(self):
        r = os.path.basename(self._path)
        return r

    def _date_from_key(self, date):
        if date is not None:
            return datetime.datetime.strptime(date, "%Y%m%d%H%M%S")

    def _date_to_key(self, date):
        if date is None:
            return 'none'
        else:
            return datetime.datetime.strftime(date, "%Y%m%d%H%M%S")

    def _date_check(self, value, needed_date, value_name, needed_date_name):
        if value is not None:
            if needed_date is None or value < needed_date:
                raise RuntimeError(
                    "Cannot set %s date before %s date on key %s on zone %s" % (
                        value_name,
                        needed_date_name,
                        self.keyid,
                        self.zone_name
                     )
                )

    def _date_check2(self, value, needed_date, value_name, needed_date_name):
        msg = "Cannot set %s date after %s date on key %s on zone %s" % (
                  value_name,
                  needed_date_name,
                  self.keyid,
                  self.zone_name
              )
        if value is None and needed_date is not None:
            raise RuntimeError(msg)
        elif value is not None and needed_date is not None:
            if value > needed_date:
                raise RuntimeError(msg)

    @classmethod
    def create(cls, typ, name, options=None):
        if options is None:
            options = []
        path = os.path.join(BASE, name)
        cmd = ["/usr/sbin/dnssec-keygen", "-a", "RSASHA256"]
        if typ == "KSK":
            cmd.extend(["-b", "2048", "-f", "KSK"])
        elif typ == "ZSK":
            cmd.extend(["-b", "1024"])
        else:
            raise ValueError("typ must be KSK or ZSK")
        cmd.extend(options)
        cmd.extend(["-K", path,  name])
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
        p.wait()
        if p.returncode != 0:
            raise ValueError("La creation de la clef a echoue")
        keyname = p.communicate()[0].strip()
        bind_chown(path)
        return cls(os.path.join(path, "%s.private" % keyname))

    def gen_successor(self):
        cmd = [
            "/usr/sbin/dnssec-keygen", "-i", str(int(INTERVAL.total_seconds())),
            "-S", self._path, "-K", os.path.dirname(self._path)
        ]
        p = subprocess.Popen(cmd, stderr=subprocess.PIPE)
        err = p.communicate()[1]
        if p.returncode != 0:
            raise ValueError("err %s: %s" % (p.returncode, err))
        if err:
            print err
        bind_chown(os.path.dirname(self._path))

    @property
    def created(self):
        if self._created is not None:
            return self._date_from_key(self._created)

    @property
    def publish(self):
        if self._publish is not None:
            return self._date_from_key(self._publish)
    @publish.setter
    def publish(self, value):
        self._date_check(value, self.created, "publish", "created")
        self._date_check2(value, self.activate, "publish", "activate")
        date = self._date_to_key(value)
        if date != self._publish:
            settime(self._path, 'P', date)
            self._publish = date
            with open(self._path, 'r') as f:
                self._data = f.read()

    @property
    def activate(self):
        if self._activate is not None:
            return self._date_from_key(self._activate)
    @activate.setter
    def activate(self, value):
        self._date_check(value, self.publish, "active", "publish")
        self._date_check2(value, self.inactive, "activate", "inactive")
        date = self._date_to_key(value)
        if date != self._activate:
            settime(self._path, 'A', date)
            self._activate = date
            with open(self._path, 'r') as f:
                self._data = f.read()

    @property
    def inactive(self):
        if self._inactive is not None:
            return self._date_from_key(self._inactive)
    @inactive.setter
    def inactive(self, value):
        self._date_check(value, self.activate, "inactive", "activate")
        self._date_check2(value, self.delete, "inactive", "delete")
        date = self._date_to_key(value)
        if date != self._inactive:
            settime(self._path, 'I', date)
            self._inactive = date
            with open(self._path, 'r') as f:
                self._data = f.read()

    @property
    def delete(self):
        if self._delete:
            return self._date_from_key(self._delete)
    @delete.setter
    def delete(self, value):
        self._date_check(value, self.inactive, "delete", "inactive")
        date = self._date_to_key(value)
        if date != self._delete:
            settime(self._path, 'D', date)
            self._delete = date
            with open(self._path, 'r') as f:
                self._data = f.read()

    @property
    def is_publish(self):
        return self.publish is not None and self.publish <= datetime.datetime.utcnow()

    @property
    def is_activate(self):
        return self.activate is not None and self.activate <= datetime.datetime.utcnow()

    @property
    def is_inactive(self):
        return self.inactive is not None and self.inactive <= datetime.datetime.utcnow()

    @property
    def is_delete(self):
        return self.delete is not None and self.delete <= datetime.datetime.utcnow()

    @property
    def need_renew(self):
        if self.type == "KSK":
            return (self.activate + KSK_VALIDITY) <= (datetime.datetime.utcnow() + INTERVAL)
        elif self.type == "ZSK":
            return (self.activate + ZSK_VALIDITY) <= (datetime.datetime.utcnow() + INTERVAL)
        else:
            raise RuntimeError("impossible")

    def __init__(self, path):
        if not path.endswith(".private"):
            raise ValueError("%s n'est pas une clef valide" % path)
        if not os.path.isfile(path):
            raise ValueError("%s n'existe pas" % path)
        ppath = "%s.key" % path[:-8]
        if not os.path.isfile(ppath):
            raise ValueError("la clef publique (%s) de %s n'existe pas" % (ppath, path))
        with open(ppath, 'r') as f:
            self._data = f.read()
        with open(path, 'r') as f:
            private_data = f.read()
        for line in self._data.split("\n"):
            if line.startswith(";") or not line:
                continue
            line = line.split(";", 1)[0].strip()
            line = line.split()
            if len(line) < 7:
                raise ValueError(
                    "La clef publique %s devrait avoir au moins 7 champs: %r" % (ppath, line)
                )
            if not line[0].endswith('.'):
                raise ValueError(
                    (
                        "La clef publique %s devrait commencer par le fqdn "
                        "(finissant par un .) de la zone"
                    ) % ppath
                )
            self.zone_name = line[0][:-1]
            try:
                self.flag = int(line[3])
            except ValueError:
                raise ValueError(
                    "Le flag %s de la clef publique %s devrait être un entier" % (line[3], ppath)
                )
        if self.flag == 256:
            self.type = "ZSK"
        elif self.flag == 257:
            self.type = "KSK"
        else:
            raise ValueError("%s n'est pas une clef valide: flag %s inconnu" % (ppath, self.flag))
        self._path = ppath
        self._path_private = path
        keyid = path.split('.')[-2].split('+')[-1]
        try:
            self.keyid = int(keyid)
        except ValueError:
            raise ValueError("Le keyid %s de la clef %s devrait être un entier" % (keyid, path))
        for line in private_data.split("\n"):
            if line.startswith("Created:"):
                self._created = line[8:].strip()
                self._date_from_key(self._created)
            elif line.startswith("Publish:"):
                self._publish = line[8:].strip()
                self._date_from_key(self._publish)
            elif line.startswith("Activate:"):
                self._activate = line[9:].strip()
                self._date_from_key(self._activate)
            elif line.startswith("Inactive:"):
                self._inactive = line[9:].strip()
                self._date_from_key(self._inactive)
            elif line.startswith("Delete:"):
                self._delete = line[7:].strip()
                self._date_from_key(self._delete)
        if self.created is None:
            raise ValueError("La clef %s doit au moins avoir le champs Created de définit" % path)

    def __lt__(self, y):
        if not isinstance(y, Key):
            raise ValueError("can only compare two Keys")
        if self.activate is not None and y.activate is not None:
            return self.activate < y.activate
        elif self.publish is not None and y.publish is not None:
            return self.publish < y.publish
        else:
            return self.created < y.created

    def __eq__(self, y):
        return isinstance(y, Key) and y._path == self._path

if __name__ == '__main__':
    try:
        parser = argparse.ArgumentParser()
        parser.add_argument('zone', nargs='*', help='zone name')
        parser.add_argument(
            '--make', '-m',
            action='store_true',
            help='Create keys for each supplied zone'
        )
        parser.add_argument(
            '--cron', '-c',
            action='store_true',
            help='Perform maintenance for each supplied zone or for all zones if no zone supplied'
        )
        parser.add_argument(
            '-ds',
            action='store_true',
            help='Show DS for each supplied zone or for all zones if no zone supplied'
        )
        parser.add_argument(
            '-key',
            action='store_true',
            help='Show DNSKEY for each zone supplied zone or for all zones if no zone supplied'
        )
        parser.add_argument(
            '--ds-seen',
            metavar='KEYID',
            type=int,
            help=(
                'To call with the ID of a new KSK published in the parent zone. '
                'Programs old KSK removal'
            )
        )
        parser.add_argument(
            '--nsec3',
            action='store_true',
            help='Enable NSEC3 for the zones, using a random salt'
        )
        args = parser.parse_args()
        zones = args.zone
        if args.make:
            for zone in zones:
                Zone.create(zone)
        zones = get_zones(zones if zones else None)
        if args.nsec3:
            for zone in zones:
                nsec3(zone.name, os.urandom(24).encode("hex"))
        if args.ds_seen:
            if len(zones) != 1:
                sys.stderr.write("Please specify exactly ONE zone name\n")
                sys.exit(1)
            for zone in zones:
                zone.ds_seen(args.ds_seen)
        if args.cron:
            for zone in zones:
                zone.do_zsk()
                zone.do_ksk()
                zone.remove_deleted()
        if args.ds:
            for zone in zones:
                zone.ds()
        if args.key:
            for zone in zones:
                zone.key()
        if not any([args.make, args.cron, args.ds, args.key, args.ds_seen, args.nsec3]):
            parser.print_help()
    except ValueError as error:
        sys.stderr.write("%s\n" % error)
        sys.exit(1)
