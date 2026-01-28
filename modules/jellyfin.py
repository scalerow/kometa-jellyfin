import logging
from jellyfin_apiclient_python import JellyfinClient
from typing import List, Optional, Dict
from modules.library import Library
import os, re, time
from datetime import datetime, timedelta
from modules import builder, util
from modules.library import Library
from modules.poster import ImageData
from modules.request import parse_qs, quote_plus, urlparse
from modules.util import Failed
from PIL import Image
from requests.exceptions import ConnectionError, ConnectTimeout
from tenacity import retry, stop_after_attempt, wait_fixed, retry_if_not_exception_type
from xml.etree.ElementTree import ParseError

logger = util.logger

class Jellyfin(Library):
    """
    Minimal client wrapper for Jellyfin that mirrors the behavior expected from Kometa's Plex interface.
    """
    def __init__(self, config, params):
        # Initialize API client
        self.client = JellyfinClient()
        self.jellyfin = params["jellyfin"]
        self.url = self.jellyfin["url"]
        self.token = self.jellyfin["token"]
        if self.jellyfin["verify_ssl"] is False:
            self.client.config.data["auth.ssl"] = False
        self.client.config.data["app.name"] = 'kometa-jellyfin'
        self.client.config.data["app.version"] = '0.0.1'
        self.client.authenticate({"Servers": [{"AccessToken": self.token, "address": self.url}]}, discover=False)

        self.api = self.client.jellyfin
        logger.info(f"Connected to Jellyfin at {self.jellyfin["url"]}")
        logger.secret(self.url)
        logger.secret(self.token)

    # --- Basic data fetchers ---
    def get_users(self) -> List[Dict]:
        """Fetch all users (mostly for mapping metadata ownership)."""
        return self.api.users_get_users()

    def get_libraries(self) -> List[Dict]:
        """List top-level libraries."""
        data = self.api.library_get_media_folders()
        return data.get("Items", [])

    def get_items(self, parent_id: Optional[str] = None, item_types: Optional[List[str]] = None) -> List[Dict]:
        """Fetch items from library or folder."""
        params = {"Recursive": True}
        if parent_id:
            params["ParentId"] = parent_id
        if item_types:
            params["IncludeItemTypes"] = ",".join(item_types)
        return self.api.items_get_items(**params).get("Items", [])

    def search(self, term: str, item_types: Optional[List[str]] = None) -> List[Dict]:
        """Search for items by name."""
        params = {"SearchTerm": term}
        if item_types:
            params["IncludeItemTypes"] = ",".join(item_types)
        return self.api.items_get_items(**params).get("Items", [])

    # --- Collections ---
    def get_collections(self) -> List[Dict]:
        """Get all collections."""
        data = self.api.collections_get_collections()
        return data.get("Items", [])

    def create_collection(self, name: str, item_ids: List[str]) -> str:
        """Create a new collection and add items."""
        payload = {"Name": name, "Ids": item_ids}
        result = self.api.collections_add_to_collection(**payload)
        return result.get("Id")

    def add_to_collection(self, collection_id: str, item_ids: List[str]):
        """Add items to an existing collection."""
        payload = {"Ids": item_ids}
        self.api.collections_add_to_collection(collection_id, **payload)

    # --- Metadata ---
    def update_item(self, item_id: str, metadata: Dict):
        """
        Update item metadata (title, overview, tags, etc.).
        Jellyfin’s API supports PATCH-like operations for a subset of fields.
        """
        self.api.items_update_item(item_id, **metadata)

    def upload_image(self, item_id: str, image_path: str, image_type: str = "Primary"):
        """Upload a poster or background image."""
        with open(image_path, "rb") as f:
            self.api.image_upload(item_id, image_type, file=f)

    # --- Utility ---
    def get_item_id_by_name(self, name: str, item_type: str = "Movie") -> Optional[str]:
        """Search for an item and return its first match ID."""
        results = self.search(name, [item_type])
        return results[0]["Id"] if results else None


    def find_poster_url(self, item):
        if isinstance(item, Movie):
            if item.ratingKey in self.movie_rating_key_map:
                return self.config.TMDb.get_movie(self.movie_rating_key_map[item.ratingKey]).poster_url
        elif isinstance(item, (Show, Season, Episode)):
            check_key = item.ratingKey if isinstance(item, Show) else item.show().ratingKey
            if check_key in self.show_rating_key_map:
                tmdb_id = self.config.Convert.tvdb_to_tmdb(self.show_rating_key_map[check_key])
                if isinstance(item, Show) and item.ratingKey in self.show_rating_key_map:
                    return self.config.TMDb.get_show(tmdb_id).poster_url
                elif isinstance(item, Season):
                    return self.config.TMDb.get_season(tmdb_id, item.seasonNumber).poster_url
                elif isinstance(item, Episode):
                    return self.config.TMDb.get_episode(tmdb_id, item.seasonNumber, item.episodeNumber).still_url


    def get_all(self, builder_level=None, load=False):
        if load and builder_level in [None, "show", "artist", "movie"]:
            self._all_items = []
        if self._all_items and builder_level in [None, "show", "artist", "movie"]:
            return self._all_items
        builder_type = builder_level if builder_level else self.Plex.TYPE
        if not builder_level:
            builder_level = self.type
        logger.info(f"Loading All {builder_level.capitalize()}s from Library: {self.name}")
        key = f"/library/sections/{self.Plex.key}/all?includeGuids=1&type={utils.searchType(builder_type)}"
        container_start = 0
        container_size = plexapi.X_PLEX_CONTAINER_SIZE
        results = []
        total_size = 1
        while total_size > len(results) and container_start <= total_size:
            data = self.Plex._server.query(key, headers={"X-Plex-Container-Start": str(container_start), "X-Plex-Container-Size": str(container_size)})
            subresults = self.Plex.findItems(data, initpath=key)
            total_size = utils.cast(int, data.attrib.get('totalSize') or data.attrib.get('size')) or len(subresults)

            librarySectionID = utils.cast(int, data.attrib.get('librarySectionID'))
            if librarySectionID:
                for item in subresults:
                    item.librarySectionID = librarySectionID

            results.extend(subresults)
            container_start += container_size
            logger.ghost(f"Loaded: {total_size if container_start > total_size else container_start}/{total_size}")

        logger.info(f"Loaded {total_size} {builder_level.capitalize()}s")
        if builder_level in [None, "show", "artist", "movie"]:
            self._all_items = results
        return results

    def image_update(self, item, image, tmdb=None, title=None, poster=True):
        text = f"{f'{title} ' if title else ''}{'Poster' if poster else 'Background'}"
        attr = self.mass_poster_update["source"] if poster else self.mass_background_update["source"]
        if attr == "lock":
            self.query(item.lockPoster if poster else item.lockArt)
            logger.info(f"{text} | Locked")
        elif attr == "unlock":
            self.query(item.unlockPoster if poster else item.unlockArt)
            logger.info(f"{text} | Unlocked")
        else:
            location = "the Assets Directory" if image else ""
            image_url = False if image else True
            image = image.location if image else None
            if not image:
                if attr == "tmdb" and tmdb:
                    image = tmdb
                    location = "TMDb"
                if not image:
                    images = item.posters() if poster else item.arts()
                    temp_image = next((p for p in images), None)
                    if temp_image:
                        if temp_image.key.startswith("/"):
                            image = f"{self.url}{temp_image.key}&X-Plex-Token={self.token}"
                        else:
                            image = temp_image.key
                        location = "Plex"
            if image:
                logger.info(f"{text} | Reset from {location}")
                if poster:
                    try:
                        self.upload_poster(item, image, url=image_url)
                    except Exception as e:
                        logger.stacktrace()
                        logger.error(f"Plex Error: {e}")
                else:
                    try:
                        self.upload_background(item, image, url=image_url)
                    except Exception as e:
                        logger.stacktrace()
                        logger.error(f"Plex Error: {e}")
                if poster and "Overlay" in [la.tag for la in self.item_labels(item)]:
                    logger.info(self.edit_tags("label", item, remove_tags="Overlay", do_print=False))
            else:
                logger.warning(f"{text} | No Reset Image Found")

    def item_labels(self, item):
        try:
            return item.labels
        except Exception:
            raise Failed(f"Item: {item.title} Labels failed to load")

    def find_poster_url(self, item):
        if isinstance(item, Movie):
            if item.ratingKey in self.movie_rating_key_map:
                return self.config.TMDb.get_movie(self.movie_rating_key_map[item.ratingKey]).poster_url
        elif isinstance(item, (Show, Season, Episode)):
            check_key = item.ratingKey if isinstance(item, Show) else item.show().ratingKey
            if check_key in self.show_rating_key_map:
                tmdb_id = self.config.Convert.tvdb_to_tmdb(self.show_rating_key_map[check_key])
                if isinstance(item, Show) and item.ratingKey in self.show_rating_key_map:
                    return self.config.TMDb.get_show(tmdb_id).poster_url
                elif isinstance(item, Season):
                    return self.config.TMDb.get_season(tmdb_id, item.seasonNumber).poster_url
                elif isinstance(item, Episode):
                    return self.config.TMDb.get_episode(tmdb_id, item.seasonNumber, item.episodeNumber).still_url

    def item_posters(self, item, providers=None):
        if providers is None:
            providers = ["plex", "tmdb"]
        image_url = None
        for provider in providers:
            if provider == "plex":
                for poster in item.posters():
                    if poster.key.startswith("/"):
                        image_url = f"{self.url}{poster.key}&X-Plex-Token={self.token}"
                        if poster.ratingKey.startswith("upload"):
                            try:
                                self.check_image_for_overlay(image_url, os.path.join(self.overlay_backup, "temp"), remove=True)
                            except Failed as e:
                                logger.trace(f"Plex Error: {e}")
                                continue
                    else:
                        image_url = poster.key
                    break
            if provider == "tmdb":
                try:
                    image_url = self.find_poster_url(item)
                except Failed as e:
                    logger.trace(e)
                    continue
            if image_url:
                break
        if not image_url and "plex" in providers and isinstance(item, Season):
            for poster in item.show().posters():
                if poster.key.startswith("/"):
                    image_url = f"{self.url}{poster.key}&X-Plex-Token={self.token}"
                    if poster.ratingKey.startswith("upload"):
                        try:
                            self.check_image_for_overlay(image_url, os.path.join(self.overlay_backup, "temp"), remove=True)
                        except Failed as e:
                            logger.trace(f"Plex Error: {e}")
                            continue
                else:
                    image_url = poster.key
                break
        if not image_url:
            raise Failed("Overlay Error: No Poster found to reset")
        return image_url

    def notify(self, text, collection=None, critical=True):
        self.config.notify(text, server=self.PlexServer.friendlyName, library=self.name, collection=collection, critical=critical)

    def notify_delete(self, message):
        self.config.notify_delete(message, server=self.PlexServer.friendlyName, library=self.name)

    @retry(stop=stop_after_attempt(6), wait=wait_fixed(10), retry=retry_if_not_exception_type((Exception, Exception, Exception)))
    def reload(self, item, force=False):
        is_full = False
        if not force and item.ratingKey in self.cached_items:
            item, is_full = self.cached_items[item.ratingKey]
        try:
            if not is_full or force:
                self.item_reload(item)
                self.cached_items[item.ratingKey] = (item, True)
        except (Exception, Exception) as e:
            logger.stacktrace()
            raise Failed(f"Item Failed to Load: {e}")
        return item

    @retry(stop=stop_after_attempt(6), wait=wait_fixed(10), retry=retry_if_not_exception_type((Exception, Exception, Exception)))
    def upload_poster(self, item, image, url=False):
        if url:
            item.uploadPoster(url=image)
        else:
            item.uploadPoster(filepath=image)

    @retry(stop=stop_after_attempt(6), wait=wait_fixed(10), retry=retry_if_not_exception_type((Exception, Exception, Exception)))
    def _upload_image(self, item, image):
        upload_success = True
        try:
            if image.is_url and "theposterdb.com" in image.location:
                now = datetime.now()
                if self.config.tpdb_timer is not None:
                    while self.config.tpdb_timer + timedelta(seconds=6) > now:
                        time.sleep(1)
                        now = datetime.now()
                self.config.tpdb_timer = now
            if image.is_poster and image.is_url:
                item.uploadPoster(url=image.location)
            elif image.is_poster:
                upload_success = self.validate_image_size(image)
                if upload_success:
                    item.uploadPoster(filepath=image.location)
            elif image.is_background and image.is_url:
                item.uploadArt(url=image.location)
            elif image.is_background:
                upload_success = self.validate_image_size(image)
                if upload_success:
                    item.uploadArt(filepath=image.location)
            elif image.is_url:
                item.uploadLogo(url=image.location)
            else:
                item.uploadLogo(filepath=image.location)
            self.reload(item, force=True)
            return upload_success
        except Exception as e:
            item.refresh()
            raise Failed(e)

    def edit_tags(self, attr, obj, add_tags=None, remove_tags=None, sync_tags=None, do_print=True, locked=True, is_locked=None):
        display = ""
        final = ""
        key = attribute_translation[attr] if attr in attribute_translation else attr
        actual = "similar" if attr == "similar_artist" else attr
        attr_display = attr.replace("_", " ").title()
        if add_tags or remove_tags or sync_tags is not None:
            _add_tags = add_tags if add_tags else []
            _remove_tags = remove_tags if remove_tags else []
            _sync_tags = sync_tags if sync_tags else []
            try:
                obj = self.reload(obj)
                _item_tags = [item_tag.tag for item_tag in getattr(obj, key)]
            except Exception:
                _item_tags = []
            _add = [t for t in _add_tags + _sync_tags if t not in _item_tags]
            _remove = [t for t in _item_tags if (sync_tags is not None and t not in _sync_tags) or t in _remove_tags]
            if _add:
                self.tag_edit(obj, actual, _add, locked=locked)
                display += f"+{', +'.join(_add)}"
            if _remove:
                self.tag_edit(obj, actual, _remove, locked=locked, remove=True)
                if display:
                    display += ", "
                display += f"-{', -'.join(_remove)}"
            if is_locked is not None and not display and is_locked != locked:
                self.edit_query(obj, {f"{actual}.locked": 1 if locked else 0})
                display = "Locked" if locked else "Unlocked"
            final = f"{obj.title[:25]:<25} | {attr_display} | {display}" if display else display
            if do_print and final:
                logger.info(final)
        return final[28:] if final else final

    def find_poster_url(self, item):
        if isinstance(item, Movie):
            if item.ratingKey in self.movie_rating_key_map:
                return self.config.TMDb.get_movie(self.movie_rating_key_map[item.ratingKey]).poster_url
        elif isinstance(item, (Show, Season, Episode)):
            check_key = item.ratingKey if isinstance(item, Show) else item.show().ratingKey
            if check_key in self.show_rating_key_map:
                tmdb_id = self.config.Convert.tvdb_to_tmdb(self.show_rating_key_map[check_key])
                if isinstance(item, Show) and item.ratingKey in self.show_rating_key_map:
                    return self.config.TMDb.get_show(tmdb_id).poster_url
                elif isinstance(item, Season):
                    return self.config.TMDb.get_season(tmdb_id, item.seasonNumber).poster_url
                elif isinstance(item, Episode):
                    return self.config.TMDb.get_episode(tmdb_id, item.seasonNumber, item.episodeNumber).still_url




