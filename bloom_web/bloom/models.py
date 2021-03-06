from django.contrib import admin
from django.db import models
from django.contrib.auth.models import User
from django.conf import settings
from django.core.files import File
from django.core.files.storage import FileSystemStorage
from tagging.models import Tag,TaggedItem
import hashlib
import shutil
import string
import os
import logging
import itertools
import datetime
logging.basicConfig(level=logging.DEBUG)


in_dir = getattr(settings, "BLOOM_IN_PATH", "/tmp/bloom/in")
out_dir = getattr(settings, "BLOOM_OUT_PATH", "/tmp/bloom/out")
media_path = getattr(settings, "BLOOM_MEDIA_PATH", "/logs/")

fs = FileSystemStorage(location=out_dir, base_url=media_path)

def md5_for_file(file_name, block_size=2**20):
    try:
        with open(file_name) as f:
            md5 = hashlib.md5()
            while True:
                data = f.read(block_size)
                if not data:
                    break
                md5.update(data)
            return md5.hexdigest()
    except IOError:
        return None

def read_tag_file(file_name):
    with open(file_name) as f:
        lines = f.readlines()
        num, p1_name, p2_name, winner_index = lines[0][0:-1].split(', ')
        num = int(num)
        winner_index = int(winner_index)

        p1_name = string.replace(p1_name, ' ', '')
        p2_name = string.replace(p2_name, ' ', '')

        p1 = GamePlayerInfo.objects.create(player=(User.objects.get(username__iexact=p1_name)),
                                           winner=(winner_index==0))
        
        Tag.objects.update_tags(p1, lines[1])

        p2 = GamePlayerInfo.objects.create(player=(User.objects.get(username__iexact=p2_name)),
                                           winner=(winner_index==1))
        Tag.objects.update_tags(p2, lines[2])

        return num, p1, p2

class UserStat(models.Model):
    user = models.OneToOneField(User, related_name="stats")
    games = models.IntegerField(default=0)
    wins = models.IntegerField(default=0)
    losses = models.IntegerField(default=0)

    def __unicode__(self):
        return "%s (%u/%u)" % (self.user.username, self.wins, self.games)

    class Meta:
        ordering = ("-wins", "-games")

    @staticmethod
    def populate_stats():
        for user in User.objects.all():
            games, wins, losses = 0,0,0
            stats, created = UserStat.objects.get_or_create(user=user)
            for game in GameLog.mine_with_win(user):
                games += 1
                if game.win_status == "win":
                    wins += 1
                if game.win_status == "loss":
                    losses += 1
            stats.games = games
            stats.wins = wins
            stats.losses = losses
            stats.save()

    def ratio(self):
        if not(self.games):
            return 0
        else:
            return int(100*float(self.wins)/self.games)
            
class GamePlayerInfo(models.Model):
    player = models.ForeignKey(User)
    winner = models.BooleanField()

    def __unicode__(self):
        return "%s (%u)" % (self.player.username, self.pk)
    
class GameLog(models.Model):
    game_hash = models.CharField(max_length=256)
    file = models.FileField(upload_to=out_dir, storage=fs)
    number = models.IntegerField()
    p1 = models.OneToOneField(GamePlayerInfo, related_name='p1_log_set')
    p2 = models.OneToOneField(GamePlayerInfo, related_name='p2_log_set')
    timestamp = models.DateTimeField(auto_now_add=True, default=datetime.datetime.now)
    class Meta:
        ordering = ["-id"]
        
    def __unicode__(self):
        return "%s vs. %s" % (self.p1.player.username, self.p2.player.username)

    @classmethod
    def mine(cls, user):
        try:
            return cls.objects.filter(models.Q(p1__player=user) | models.Q(p2__player=user)).select_related()
        except cls.DoesNotExist:
            return None

    @classmethod
    def ours(cls, me, them):
        try:
            return cls.objects.filter(models.Q(p1__player=me, p2__player=them) |
                                      models.Q(p1__player=them, p2__player=me)).select_related()
        except cls.DoesNotExist:
            return None

    @staticmethod
    def ours_with_data(me, them):
        with_tags = GameLog.add_tags(GameLog.ours(me, them),me)
        return GameLog.add_win_status(with_tags, me)

    @staticmethod
    def mine_with_tag(me, tag):
        my_logs = GameLog.mine(me)
        with_tags = GameLog.add_tags(my_logs, me)
        filtered = itertools.ifilter(lambda l: tag in l.tags, with_tags)
        return GameLog.add_win_status(filtered, me)

    @staticmethod
    def all_with_tag(tag):
        my_logs = GameLog.objects.all().select_related()
        with_tags = GameLog.combine_tags(my_logs)
        filtered = itertools.ifilter(lambda l: tag in l.tags, with_tags)
        return GameLog.winner(filtered)
    
    @staticmethod
    def add_win_status(qs, user):
        for q in qs:
            if q.p1.player==user and q.p2.player==user:
                q.win_status = "tie"
            else:
                if q.p1.player==user:
                    q.win_status = ["loss", "win"][q.p1.winner]
                else:
                    q.win_status = ["win", "loss"][q.p1.winner]
            yield q
        return
    
    @staticmethod
    def add_tags(qs, user):
        for q in qs:
            q.tags = set(itertools.chain.from_iterable(p.tags for p in [q.p1, q.p2] if p.player==user))
            yield q
        return

    @staticmethod
    def mine_with_win(user):
        return GameLog.add_win_status(GameLog.mine(user), user)

    @staticmethod
    def my_objects(user):
        objs = GameLog.mine_with_win(user)
        return GameLog.add_tags(objs, user)

    @staticmethod
    def combine_tags(qs):
        for q in qs:
            q.tags = set(itertools.chain.from_iterable(p.tags for p in [q.p1, q.p2]))
            yield q
        return

    @staticmethod
    def winner(qs):
        for q in qs:
            q.win_status = q.p1.player.username if q.p1.winner else q.p2.player.username
            yield q
        return

    @staticmethod
    def objects_with_tags():
        objs = GameLog.objects.all()
        return GameLog.winner(GameLog.combine_tags(objs))
    

    @staticmethod
    def create_new(log_file, tag_file):
        game_hash = md5_for_file(log_file)
        if not(game_hash):
            """The file doesn't exist"""
            return None
        try:
            number, p1, p2 = read_tag_file(tag_file)
        except (IOError, User.DoesNotExist):
            return None

        """Copy the file to the output directory"""
        new_file_location = os.path.join(out_dir, "%s.gamelog"%game_hash)
        shutil.move(log_file, new_file_location)

        gl = GameLog(game_hash=game_hash,
                     number=number,
                     p1=p1,
                     p2=p2)
        gl.file.name = '%s.gamelog'%game_hash
        gl.save()
        return gl

