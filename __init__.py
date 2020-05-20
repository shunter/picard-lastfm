# -*- coding: utf-8 -*-

PLUGIN_NAME = 'Last.fm'
PLUGIN_AUTHOR = 'Lukáš Lalinský, Philipp Wolfer'
PLUGIN_DESCRIPTION = 'Use tags from Last.fm as genre.'
PLUGIN_VERSION = "0.10"
PLUGIN_API_VERSIONS = ["2.0"]

from functools import partial
from PyQt5 import QtCore
from picard import config, log
from picard.config import BoolOption, IntOption, TextOption
from picard.metadata import register_track_metadata_processor
from picard.plugins.lastfm.ui_options_lastfm import Ui_LastfmOptionsPage
from picard.ui.options import register_options_page, OptionsPage
from picard.webservice import ratecontrol
from picard.util.textencoding import asciipunct
import os

from .titlecase import titlecase

LASTFM_HOST = 'ws.audioscrobbler.com'
LASTFM_PORT = 80
LASTFM_PATH = '/2.0/'
LASTFM_API_KEY = '0a210a4a6741f2ec8f27a791b9d5d971'

# From https://www.last.fm/api/tos, 2018-09-04
# 4.4 […] You will not make more than 5 requests per originating IP address per
# second, averaged over a 5 minute period, without prior written consent. […]
ratecontrol.set_minimum_delay((LASTFM_HOST, LASTFM_PORT), 200)

# Cache for Tags to avoid re-requesting tags within same Picard session
_cache = {}

# Keeps track of requests for tags made to webservice API but not yet returned
# (to avoid re-requesting the same URIs)
_pending_requests = {}

class Processor:
    def __init__(self, album, metadata, track, release):
        self.album = album
        self.metadata = metadata

        setting = config.setting
        self.min_tag_usage = setting["lastfm_min_tag_usage"]
        self.join_tags = setting["lastfm_join_tags"]

        with (open(os.path.join(os.path.dirname(__file__), 'ignore_tags.txt'))) as f:
            lines = f.read().splitlines()
            self.ignore_tags = set(map(lambda x: x.lower(), lines))

        self.artist_tags = None
        self.track_tags = None
        self.album_tags = None

        artist = asciipunct(metadata["artist"])
        title = asciipunct(metadata["title"])
        album = asciipunct(metadata["album"])
        albumartist = asciipunct(metadata["albumartist"])

        params = dict(
            method="artist.gettoptags",
            artist=artist)
        cachekey = 'ar-' + artist
        self.get_tags(params, cachekey, self.set_artist_tags)

        params = dict(
            method="track.gettoptags",
            track=title,
            artist=artist)
        cachekey = 't-' + artist + '-' + title
        self.get_tags(params, cachekey, self.set_track_tags)

        params = dict(
            method="album.gettoptags",
            album=album,
            artist=albumartist)
        cachekey = 'al-' + album + '-' + albumartist
        self.get_tags(params, cachekey, self.set_album_tags)

    def set_artist_tags(self, tags):
        self.artist_tags = tags
        self.tags_finalize()

    def set_track_tags(self, tags):
        self.track_tags = tags
        self.tags_finalize()

    def set_album_tags(self, tags):
        self.album_tags = tags
        self.tags_finalize()

    def tags_finalize(self):
        if (self.artist_tags is None or self.track_tags is None or self.album_tags is None):
            return

        tags = self.track_tags + self.album_tags + self.artist_tags

        set = {}
        tags = [set.setdefault(e,e) for e in tags if e not in set]

        join_tags = config.setting["lastfm_join_tags"]
        if join_tags:
            combined = ""
            for idx, tag in enumerate(tags):
                if (idx > 0):
                    tag = join_tags + tag
                if (len(combined) + len(tag)) < 255:
                    combined += tag
            tags = combined

        self.metadata["genre"] = tags

    def get_tags(self, params, cachekey, set_tags):
        if cachekey in _cache:
            set_tags(_cache[cachekey])
        else:
            # If we have already sent a request for this URL, delay this call
            if cachekey in _pending_requests:
                _pending_requests[cachekey].append(set_tags)
            else:
                _pending_requests[cachekey] = []
                self.album._requests += 1
                params.update(dict(api_key=LASTFM_API_KEY))
                queryargs = {k: bytes(QtCore.QUrl.toPercentEncoding(v)).decode() for k, v in params.items()}
                self.album.tagger.webservice.get(
                    LASTFM_HOST, LASTFM_PORT, LASTFM_PATH,
                    partial(self.tags_downloaded, cachekey, set_tags),
                    queryargs=queryargs, parse_response_type='xml',
                    priority=True, important=False)

    def tags_downloaded(self, cachekey, set_tags, data, http, error):
        if error:
            self.album._requests -= 1
            self.album._finalize_loading(None)
            return

        #self.album.tagger.log.info("tags_downloaded: %s", http.url())
        try:
            ignore = self.ignore_tags
            tags = []
            try:
                lfm = data.lfm.pop()

                if lfm.attribs['status'] == 'failed':
                    error = lfm.error.pop()
                    self.album.tagger.log.error("lfm api error: {0} - {1} - {2}".format(error.attribs['code'], error.text, str(http.url())))
                    return

                toptags = lfm.toptags.pop()

                try: ignore.add(toptags.artist.lower())
                except AttributeError: pass
                try: ignore.add(toptags.track.lower())
                except AttributeError: pass

                for tag in toptags.tag:
                    name = tag.name[0].text.strip().lower()

                    try: count = int(tag.count[0].text.strip())
                    except ValueError: count = 0

                    if count < self.min_tag_usage:
                        break

                    if name not in ignore:
                        tags.append(titlecase(name))
            except AttributeError:
                pass

            _cache[cachekey] = tags
            set_tags(tags)

            # Process any pending requests for the same URL
            if cachekey in _pending_requests:
                pending = _pending_requests[cachekey]
                del _pending_requests[cachekey]
                for delayed_call in pending:
                    delayed_call(tags)

        except Exception:
            log.error('Problem processing download tags', exc_info=True)
        finally:
            self.album._requests -= 1
            self.album._finalize_loading(None)

def process_track(album, metadata, track, release):
    Processor(album, metadata, track, release)

class LastfmOptionsPage(OptionsPage):

    NAME = "lastfm"
    TITLE = "Last.fm"
    PARENT = "plugins"

    options = [
        BoolOption("setting", "lastfm_use_track_tags", False),
        BoolOption("setting", "lastfm_use_artist_tags", False),
        IntOption("setting", "lastfm_min_tag_usage", 90),
        TextOption("setting", "lastfm_join_tags", ""),
    ]

    def __init__(self, parent=None):
        super(LastfmOptionsPage, self).__init__(parent)
        self.ui = Ui_LastfmOptionsPage()
        self.ui.setupUi(self)

    def load(self):
        setting = config.setting
        self.ui.use_track_tags.setChecked(setting["lastfm_use_track_tags"])
        self.ui.use_artist_tags.setChecked(setting["lastfm_use_artist_tags"])
        self.ui.min_tag_usage.setValue(setting["lastfm_min_tag_usage"])
        self.ui.join_tags.setEditText(setting["lastfm_join_tags"])

    def save(self):
        setting = config.setting
        setting["lastfm_use_track_tags"] = self.ui.use_track_tags.isChecked()
        setting["lastfm_use_artist_tags"] = self.ui.use_artist_tags.isChecked()
        setting["lastfm_min_tag_usage"] = self.ui.min_tag_usage.value()
        setting["lastfm_join_tags"] = str(self.ui.join_tags.currentText())


register_track_metadata_processor(process_track)
register_options_page(LastfmOptionsPage)
