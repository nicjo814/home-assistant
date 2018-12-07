"""
Support for LinkPlay based devices.

For more details about this platform, please refer to the documentation at
https://home-assistant.io/components/media_player.linkplay/
"""

import json
import logging
import urllib.request

import requests
import voluptuous as vol

from homeassistant.components.media_player import (
    DOMAIN, MEDIA_PLAYER_SCHEMA, MEDIA_TYPE_MUSIC, PLATFORM_SCHEMA,
    SUPPORT_NEXT_TRACK, SUPPORT_PAUSE, SUPPORT_PLAY, SUPPORT_PLAY_MEDIA,
    SUPPORT_PREVIOUS_TRACK, SUPPORT_SEEK, SUPPORT_SELECT_SOURCE,
    SUPPORT_SELECT_SOUND_MODE, SUPPORT_SHUFFLE_SET, SUPPORT_VOLUME_MUTE,
    SUPPORT_VOLUME_SET, MediaPlayerDevice)
from homeassistant.const import (
    ATTR_ENTITY_ID, CONF_HOST, CONF_NAME, STATE_PAUSED, STATE_PLAYING,
    STATE_UNKNOWN)
from homeassistant.exceptions import PlatformNotReady
import homeassistant.helpers.config_validation as cv
from homeassistant.util.dt import utcnow

REQUIREMENTS = ['eyeD3==0.8.7']

_LOGGER = logging.getLogger(__name__)

ATTR_PRESET = 'preset'
CONF_LASTFM_API_KEY = 'lastfm_api_key'
DATA_LINKPLAY = 'linkplay'
DEFAULT_NAME = 'LinkPlay device'
LASTFM_API_BASE = "http://ws.audioscrobbler.com/2.0/?method="

LINKPLAY_PRESET_BUTTON_SCHEMA = MEDIA_PLAYER_SCHEMA.extend({
    vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
    vol.Required(ATTR_PRESET): cv.positive_int
})

MAX_VOL = 100

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Required(CONF_HOST): cv.string,
    vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
    vol.Optional(CONF_LASTFM_API_KEY): cv.string
})

SERVICE_PRESET_BUTTON = 'linkplay_preset_button'

SERVICE_TO_METHOD = {
    SERVICE_PRESET_BUTTON: {
        'method': 'preset_button',
        'schema': LINKPLAY_PRESET_BUTTON_SCHEMA}
}

SUPPORT_LINKPLAY = SUPPORT_SELECT_SOURCE | SUPPORT_SELECT_SOUND_MODE | \
    SUPPORT_SHUFFLE_SET | SUPPORT_VOLUME_SET | SUPPORT_VOLUME_MUTE

SUPPORT_MEDIA_MODES_WIFI = SUPPORT_NEXT_TRACK | SUPPORT_PAUSE | \
    SUPPORT_PLAY | SUPPORT_SEEK | SUPPORT_PREVIOUS_TRACK | SUPPORT_SEEK | \
    SUPPORT_PLAY_MEDIA


SOUND_MODES = {'0': 'Normal', '1': 'Classic', '2': 'Pop', '3': 'Jazz',
               '4': 'Vocal'}
SOURCES = {'wifi': 'WiFi', 'line-in': 'Line-in', 'bluetooth': 'Bluetooth',
           'optical': 'Optical', 'udisk': 'MicroSD'}
SOURCES_MAP = {'0': 'WiFi', '10': 'WiFi', '31': 'WiFi', '40': 'Line-in',
               '41': 'Bluetooth', '43': 'Optical'}


def setup_platform(hass, config, add_entities, discovery_info=None):
    """Set up the LinkPlay device."""
    import eyed3

    if DATA_LINKPLAY not in hass.data:
        hass.data[DATA_LINKPLAY] = []

    def _service_handler(service):
        """Map services to method of Linkplay devices."""
        method = SERVICE_TO_METHOD.get(service.service)
        if not method:
            return

        params = {key: value for key, value in service.data.items()
                  if key != ATTR_ENTITY_ID}
        entity_ids = service.data.get(ATTR_ENTITY_ID)
        if entity_ids:
            target_players = [player for player in hass.data[DATA_LINKPLAY]
                              if player.entity_id in entity_ids]
        else:
            target_players = None

        for player in target_players:
            getattr(player, method['method'])(**params)

    for service in SERVICE_TO_METHOD:
        schema = SERVICE_TO_METHOD[service]['schema']
        hass.services.register(
            DOMAIN, service, _service_handler, schema=schema)

    linkplay = LinkPlayDevice(eyed3,
                              config.get(CONF_NAME),
                              config.get(CONF_HOST),
                              config.get(CONF_LASTFM_API_KEY))

    if linkplay.update() is False:
        raise PlatformNotReady

    add_entities([linkplay])
    hass.data[DATA_LINKPLAY].append(linkplay)


class LinkPlayDevice(MediaPlayerDevice):
    """Representation of a LinkPlay device."""

    def __init__(self, eyed3, name, host, lfm_api_key=None):
        """Initialize the LinkPlay device."""
        self._eyed3 = eyed3
        self._name = name
        self._host = host
        self._state = STATE_UNKNOWN
        self._volume = None
        self._source = None
        self._source_list = SOURCES.copy()
        self._sound_mode = None
        self._muted = None
        self._seek_position = None
        self._duration = None
        self._position_updated_at = None
        self._shuffle = None
        self._media_album = None
        self._media_artist = None
        self._media_title = None
        self._lpapi = LinkPlayRestData(self._host)
        self._media_image_url = None
        self._media_uri = None
        self._first_update = True
        if lfm_api_key is not None:
            self._lfmapi = LastFMRestData(lfm_api_key)
        else:
            self._lfmapi = None

    @property
    def name(self):
        """Return the name of the device."""
        return self._name

    @property
    def state(self):
        """Return the state of the device."""
        return self._state

    @property
    def volume_level(self):
        """Volume level of the media player (0..1)."""
        return int(self._volume) / MAX_VOL

    @property
    def is_volume_muted(self):
        """Return boolean if volume is currently muted."""
        return bool(int(self._muted))

    @property
    def source(self):
        """Return the current input source."""
        return self._source

    @property
    def source_list(self):
        """Return the list of available input sources."""
        return sorted(list(self._source_list.values()))

    @property
    def sound_mode(self):
        """Return the current sound mode."""
        return self._sound_mode

    @property
    def sound_mode_list(self):
        """Return the available sound modes."""
        return sorted(list(SOUND_MODES.values()))

    @property
    def supported_features(self):
        """Flag media player features that are supported."""
        return SUPPORT_LINKPLAY | SUPPORT_MEDIA_MODES_WIFI

    @property
    def media_position(self):
        """Time in seconds of current seek position."""
        return self._seek_position

    @property
    def media_duration(self):
        """Time in seconds of current song duration."""
        return self._duration

    @property
    def media_position_updated_at(self):
        """When the seek position was last updated."""
        return self._position_updated_at

    @property
    def shuffle(self):
        """Return True if shuffle mode is enabled."""
        return self._shuffle

    @property
    def media_title(self):
        """Return title of the current track."""
        return self._media_title

    @property
    def media_artist(self):
        """Return name of the current track artist."""
        return self._media_artist

    @property
    def media_album_name(self):
        """Return name of the current track album."""
        return self._media_album

    @property
    def media_image_url(self):
        """Return name the image for the current track."""
        return self._media_image_url

    @property
    def media_content_type(self):
        """Content type of current playing media."""
        return MEDIA_TYPE_MUSIC

    def turn_off(self):
        """Turn off media player."""
        self._lpapi.call('GET', 'getShutdown')
        value = self._lpapi.data
        if value != "OK":
            _LOGGER.warning("Failed to power of the device. Got response: %s",
                            value)

    def set_volume_level(self, volume):
        """Set volume level, range 0..1."""
        volume = str(round(volume * MAX_VOL))
        self._lpapi.call('GET', 'setPlayerCmd:vol:{0}'.format(str(volume)))
        value = self._lpapi.data
        if value != "OK":
            _LOGGER.warning("Failed to set volume. Got response: %s",
                            value)

    def mute_volume(self, mute):
        """Mute (true) or unmute (false) media player."""
        self._lpapi.call('GET', 'setPlayerCmd:mute:{0}'.format(str(int(mute))))
        value = self._lpapi.data
        if value != "OK":
            _LOGGER.warning("Failed to mute/unmute volume. Got response: %s",
                            value)

    def media_play(self):
        """Send play command."""
        self._lpapi.call('GET', 'setPlayerCmd:play')
        value = self._lpapi.data
        if value != "OK":
            _LOGGER.warning("Failed to start playback. Got response: %s",
                            value)

    def media_pause(self):
        """Send play command."""
        self._lpapi.call('GET', 'setPlayerCmd:pause')
        value = self._lpapi.data
        if value != "OK":
            _LOGGER.warning("Failed to pause playback. Got response: %s",
                            value)

    def media_next_track(self):
        """Send next track command."""
        self._lpapi.call('GET', 'setPlayerCmd:next')
        value = self._lpapi.data
        if value != "OK":
            _LOGGER.warning("Failed to skip to next track. Got response: %s",
                            value)

    def media_previous_track(self):
        """Send previous track command."""
        self._lpapi.call('GET', 'setPlayerCmd:prev')
        value = self._lpapi.data
        if value != "OK":
            _LOGGER.warning("Failed to skip to previous track."
                            " Got response: %s", value)

    def media_seek(self, position):
        """Send media_seek command to media player."""
        self._lpapi.call('GET', 'setPlayerCmd:seek:{0}'.format(str(position)))
        value = self._lpapi.data
        if value != "OK":
            _LOGGER.warning("Failed to seek. Got response: %s",
                            value)

    def play_media(self, media_type, media_id, **kwargs):
        """Play media from a URL or file."""
        if not media_type == MEDIA_TYPE_MUSIC:
            _LOGGER.error(
                "Invalid media type %s. Only %s is supported",
                media_type, MEDIA_TYPE_MUSIC)
            return
        self._lpapi.call('GET', 'setPlayerCmd:play:{0}'.format(media_id))
        value = self._lpapi.data
        if value != "OK":
            _LOGGER.warning("Failed to play media. Got response: %s",
                            value)

    def select_source(self, source):
        """Select input source."""
        if source == 'MicroSD':
            source = 'udisk'
        else:
            source = source.lower()
        self._lpapi.call('GET',
                         'setPlayerCmd:switchmode:{0}'.format(source))
        value = self._lpapi.data
        if value != "OK":
            _LOGGER.warning("Failed to select source. Got response: %s",
                            value)

    def select_sound_mode(self, sound_mode):
        """Set Sound Mode for device."""
        mode = list(SOUND_MODES.keys())[list(
            SOUND_MODES.values()).index(sound_mode)]
        self._lpapi.call('GET', 'setPlayerCmd:equalizer:{0}'.format(mode))
        value = self._lpapi.data
        if value != "OK":
            _LOGGER.warning("Failed to set sound mode. Got response: %s",
                            value)

    def set_shuffle(self, shuffle):
        """Change the shuffle mode."""
        mode = '2' if shuffle else '0'
        self._lpapi.call('GET', 'setPlayerCmd:loopmode:{0}'.format(mode))
        value = self._lpapi.data
        if value != "OK":
            _LOGGER.warning("Failed to change shuffle mode. Got response: %s",
                            value)

    def preset_button(self, preset):
        """Simulate pressing a physical preset button."""
        self._lpapi.call('GET', 'IOSimuKeyIn:{0}'.format(str(preset).zfill(3)))
        value = self._lpapi.data
        if value != "OK":
            _LOGGER.warning("Failed to press preset button %s. "
                            "Got response: %s", preset, value)

    def _is_playing_new_track(self, status):
        """Check if track is changed since last update."""
        return bool((int(int(status['totlen']) / 1000) != self._duration) or
                    status['Title'] != self._media_title)

    def _is_playing_mp3(self):
        """Check if the current track is an MP3 file."""
        return bool(self._media_uri.find('.mp3', len(self._media_uri)-4) != -1)

    def _is_playing_spotify(self):
        return bool(((self._media_title == 'Unknown') and
                     (self._media_artist == 'Unknown')))

    def _update_from_id3(self):
        """Update track info with eyed3."""
        try:
            filename, header = urllib.request.urlretrieve(self._media_uri)
            audiofile = self._eyed3.load(filename)
            self._media_title = audiofile.tag.title
            self._media_artist = audiofile.tag.artist
            self._media_album = audiofile.tag.album
        except urllib.error.URLError:
            self._media_title = None
            self._media_artist = None
            self._media_album = None

    def _get_lastfm_coverart(self):
        """Get cover art from last.fm."""
        self._lfmapi.call('GET',
                          'track.getInfo',
                          "artist={0}&track={1}".format(
                              self._media_artist,
                              self._media_title))
        lfmdata = json.loads(self._lfmapi.data)
        try:
            self._media_image_url = \
                    lfmdata['track']['album']['image'][2]['#text']
        except (ValueError, KeyError):
            self._media_image_url = None

    def update(self):
        """Get the latest details from the device."""
        self._lpapi.call('GET', 'getPlayerStatus')
        value = self._lpapi.data

        if value is None:
            return False

        try:
            player_status = json.loads(value)
        except ValueError:
            _LOGGER.warning("REST result could not be parsed as JSON")
            _LOGGER.debug("Erroneous JSON: %s", value)
            player_status = None

        if isinstance(player_status, dict):

            if self._first_update:
                # Get device information only at startup.
                self._first_update = False

            # Update variables that changes during playback of a track.
            self._volume = player_status['vol']
            self._muted = player_status['mute']
            self._seek_position = int(int(player_status['curpos']) / 1000)
            self._position_updated_at = utcnow()
            self._media_uri = str(bytearray.fromhex(
                player_status['iuri']).decode())
            self._state = {
                'stop': STATE_PAUSED,
                'play': STATE_PLAYING,
                'pause': STATE_PAUSED,
            }.get(player_status['status'], STATE_UNKNOWN)
            self._source = SOURCES_MAP.get(player_status['mode'],
                                           'WiFi')
            self._sound_mode = SOUND_MODES.get(player_status['eq'])
            self._shuffle = True if player_status['loop'] == '2' else False

            if self._is_playing_new_track(player_status):
                # Only do some things when a new track is playing.

                # Use track title provided by device api.
                self._media_title = str(bytearray.fromhex(
                    player_status['Title']).decode())
                self._media_artist = str(bytearray.fromhex(
                    player_status['Artist']).decode())

                if self._is_playing_spotify():
                    self._media_title = 'Spotify'
                    self._media_artist = 'Spotify'

                # Check if we are playing radio
                elif player_status['totlen'] == '0':
                    self._media_album = ""
                    self._media_image_url = None

                elif self._is_playing_mp3():
                    self._update_from_id3()
                    if self._lfmapi is not None and\
                            self._media_title is not None:
                        self._get_lastfm_coverart()
                    else:
                        self._media_image_url = None

            self._duration = int(int(player_status['totlen']) / 1000)

        else:
            _LOGGER.warning("JSON result was not a dictionary")

        return True


class LinkPlayRestData:
    """Class for handling the data retrieval from the LinkPlay device."""

    def __init__(self, host):
        """Initialize the data object."""
        self.data = None
        self._request = None
        self._host = host

    def call(self, method, cmd):
        """Get the latest data from REST service."""
        self.data = None
        self._request = None
        resource = "http://{0}/httpapi.asp?command={1}".format(self._host, cmd)
        self._request = requests.Request(method, resource).prepare()

        _LOGGER.debug("Updating from %s", self._request.url)
        try:
            with requests.Session() as sess:
                response = sess.send(
                    self._request, timeout=10)
            self.data = response.text

        except requests.exceptions.RequestException as ex:
            _LOGGER.error("Error fetching data: %s from %s failed with %s",
                          self._request, self._request.url, ex)
            self.data = None


class LastFMRestData:
    """Class for handling the data retrieval from the LinkPlay device."""

    def __init__(self, api_key):
        """Initialize the data object."""
        self.data = None
        self._request = None
        self._api_key = api_key

    def call(self, method, cmd, params):
        """Get the latest data from REST service."""
        self.data = None
        self._request = None
        resource = "{0}{1}&{2}&api_key={3}&format=json".format(
            LASTFM_API_BASE, cmd, params, self._api_key)
        self._request = requests.Request(method, resource).prepare()
        _LOGGER.debug("Updating from %s", self._request.url)

        try:
            with requests.Session() as sess:
                response = sess.send(
                    self._request, timeout=10)
            self.data = response.text

        except requests.exceptions.RequestException as ex:
            _LOGGER.error("Error fetching data: %s from %s failed with %s",
                          self._request, self._request.url, ex)
            self.data = None
