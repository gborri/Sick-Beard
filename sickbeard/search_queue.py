# Author: Nic Wolfe <nic@wolfeden.ca>
# URL: http://code.google.com/p/sickbeard/
#
# This file is part of Sick Beard.
#
# Sick Beard is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Sick Beard is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Sick Beard.  If not, see <http://www.gnu.org/licenses/>.

from __future__ import with_statement

import time
import traceback
import threading

import sickbeard
from sickbeard import db, logger, common, exceptions, helpers
from sickbeard import generic_queue, scheduler
from sickbeard import search, failed_history, history
from sickbeard import ui
from sickbeard.exceptions import ex
from sickbeard.search import pickBestResult

search_queue_lock = threading.Lock()

BACKLOG_SEARCH = 10
DAILY_SEARCH = 20
FAILED_SEARCH = 30
MANUAL_SEARCH = 40
DOWNLOADABLE_SEARCH = 5

MANUAL_SEARCH_HISTORY = []
MANUAL_SEARCH_HISTORY_SIZE = 100

class SearchQueue(generic_queue.GenericQueue):
    def __init__(self):
        generic_queue.GenericQueue.__init__(self)
        self.queue_name = "SEARCHQUEUE"

    def is_in_queue(self, show, segment):
        for cur_item in self.queue:
            if isinstance(cur_item, (BacklogQueueItem, DownloadSearchQueueItem)) and cur_item.show == show and cur_item.segment == segment:
                return True
        return False

    def is_ep_in_queue(self, segment):
        for cur_item in self.queue:
            if isinstance(cur_item, (ManualSearchQueueItem, FailedQueueItem)) and cur_item.segment == segment:
                return True
        return False
    
    def is_show_in_queue(self, show):
        for cur_item in self.queue:
            if isinstance(cur_item, (ManualSearchQueueItem, FailedQueueItem)) and cur_item.show.indexerid == show:
                return True
        return False
    
    def get_all_ep_from_queue(self, show):
        ep_obj_list = []
        for cur_item in self.queue:
            if isinstance(cur_item, (ManualSearchQueueItem, FailedQueueItem)) and str(cur_item.show.indexerid) == show:
                ep_obj_list.append(cur_item)
        
        if ep_obj_list:
            return ep_obj_list
        return False
    
    def pause_backlog(self):
        self.min_priority = generic_queue.QueuePriorities.HIGH

    def pause_downloadableSearch(self):
        self.min_priority = generic_queue.QueuePriorities.HIGH

    def unpause_backlog(self):
        self.min_priority = 0

    def unpause_downloadableSearch(self):
        self.min_priority = 0

    def is_backlog_paused(self):
        # backlog priorities are NORMAL, this should be done properly somewhere
        return self.min_priority >= generic_queue.QueuePriorities.NORMAL

    def is_downloadable_search_paused(self):
        # backlog priorities are NORMAL, this should be done properly somewhere
        return self.min_priority >= generic_queue.QueuePriorities.NORMAL

    def is_manualsearch_in_progress(self):
        # Only referenced in webserve.py, only current running manualsearch or failedsearch is needed!!
        if isinstance(self.currentItem, (ManualSearchQueueItem, FailedQueueItem)):
            return True
        return False
    
    def is_backlog_in_progress(self):
        for cur_item in self.queue + [self.currentItem]:
            if isinstance(cur_item, BacklogQueueItem):
                return True
        return False

    def is_dailysearch_in_progress(self):
        for cur_item in self.queue + [self.currentItem]:
            if isinstance(cur_item, DailySearchQueueItem):
                return True
        return False

    def is_downloadable_search_in_progress(self):
        for cur_item in self.queue + [self.currentItem]:
            if isinstance(cur_item, DownloadSearchQueueItem):
                return True
        return False

    def queue_length(self):
        length = {'backlog': 0, 'downloadable_search' : 0, 'daily': 0, 'manual': 0, 'failed': 0}
        for cur_item in self.queue:
            if isinstance(cur_item, DailySearchQueueItem):
                length['daily'] += 1
            elif isinstance(cur_item, BacklogQueueItem):
                length['backlog'] += 1
            elif isinstance(cur_item, DownloadSearchQueueItem):
                length['downloadable_search'] += 1
            elif isinstance(cur_item, ManualSearchQueueItem):
                length['manual'] += 1
            elif isinstance(cur_item, FailedQueueItem):
                length['failed'] += 1
        return length


    def add_item(self, item):
        if isinstance(item, DailySearchQueueItem):
            # daily searches
            generic_queue.GenericQueue.add_item(self, item)
        elif isinstance(item, (BacklogQueueItem, DownloadSearchQueueItem)) and not self.is_in_queue(item.show, item.segment):
            # backlog searches
            generic_queue.GenericQueue.add_item(self, item)
        elif isinstance(item, (ManualSearchQueueItem, FailedQueueItem)) and not self.is_ep_in_queue(item.segment):
            # manual and failed searches
            generic_queue.GenericQueue.add_item(self, item)
        else:
            logger.log(u"Not adding item, it's already in the queue", logger.DEBUG)


class DownloadSearchQueueItem(generic_queue.QueueItem):
    def __init__(self, show, segment):
        generic_queue.QueueItem.__init__(self, 'Downloadable Search', DOWNLOADABLE_SEARCH)
        self.priority = generic_queue.QueuePriorities.LOW
        self.name = 'DOWNLOADABLE_SEARCH-' + str(show.indexerid)

        self.success = None
        self.show = show
        self.segment = segment

    def run(self):
        generic_queue.QueueItem.run(self)

        try:
            logger.log("Beginning downloadable search for [" + self.show.name + "]")
            searchResult = search.searchProviders(self.show, self.segment, "skipEp", False)
            if searchResult:
                for result in searchResult:
                    # just use the first result for now
                    logger.log(u"Marking Available " + result.name + " from " + result.provider.name)
                    search.downloadableEpisode(result)

                    # give the CPU a break
                    time.sleep(common.cpu_presets[sickbeard.CPU_PRESET])
            else:
                logger.log(u"No available episodes found during downloadable search for [" + self.show.name + "]")
        except Exception:
            logger.log(traceback.format_exc(), logger.DEBUG)

        if self.success is None:
            self.success = False

        self.finish()

class DailySearchQueueItem(generic_queue.QueueItem):
    def __init__(self):
        self.success = None
        generic_queue.QueueItem.__init__(self, 'Daily Search', DAILY_SEARCH)

    def run(self):
        generic_queue.QueueItem.run(self)

        try:
            logger.log("Beginning daily search for new episodes")
            foundResults = search.searchForNeededEpisodes()

            if not len(foundResults):
                logger.log(u"No needed episodes found")
            else:
                for result in foundResults:
                    # just use the first result for now
                    if not result.episodes[0].show.paused:
                        myDB = db.DBConnection()
                        sql_selection="SELECT show_name, indexer_id, season, episode, paused FROM (SELECT * FROM tv_shows s,tv_episodes e WHERE s.indexer_id = e.showid) T1 WHERE T1.paused = 0 and T1.episode_id IN (SELECT T2.episode_id FROM tv_episodes T2 WHERE T2.showid = T1.indexer_id and T2.status in (?,?) and T2.season!=0 and airdate is not null ORDER BY T2.season,T2.episode LIMIT 1) ORDER BY T1.show_name,season,episode"
                        results = myDB.select(sql_selection, [common.SKIPPED, common.DOWNLOADABLE])
                        show_sk = [show for show in results if show["indexer_id"] == result.episodes[0].show.indexerid]
                        if not show_sk or not sickbeard.USE_TRAKT:
                            logger.log(u"Downloading " + result.name + " from " + result.provider.name)
                            self.success = search.snatchEpisode(result)
                        else:
                            sn_sk = show_sk[0]["season"]
                            ep_sk = show_sk[0]["episode"]
                            if (int(sn_sk)*100+int(ep_sk)) < (int(result.episodes[0].season)*100+int(result.episodes[0].episode)) or not show_sk:
                                logger.log(u"Mark Downloadable " + result.name + " from " + result.provider.name)
                                search.downloadableEpisode(result)
                            else:
                                logger.log(u"Downloading " + result.name + " from " + result.provider.name)
                                search.snatchEpisode(result)
                    else:
                        logger.log(u"Mark Downloadable " + result.name + " from " + result.provider.name)
                        search.downloadableEpisode(result)

                    # give the CPU a break
                    time.sleep(common.cpu_presets[sickbeard.CPU_PRESET])

            generic_queue.QueueItem.finish(self)
        except Exception:
            logger.log(traceback.format_exc(), logger.DEBUG)

        if self.success is None:
            self.success = False

        self.finish()


class ManualSearchQueueItem(generic_queue.QueueItem):
    def __init__(self, show, segment):
        generic_queue.QueueItem.__init__(self, 'Manual Search', MANUAL_SEARCH)
        self.priority = generic_queue.QueuePriorities.HIGH
        self.name = 'MANUAL-' + str(show.indexerid)
        self.success = None
        self.show = show
        self.segment = segment
        self.started = None

    def run(self):
        generic_queue.QueueItem.run(self)

        try:
            logger.log("Beginning manual search for: [" + self.segment.prettyName() + "]")
            self.started = True
            
            searchResult = search.searchProviders(self.show, [self.segment], "wantEP", True)

            if searchResult:
                # just use the first result for now
                logger.log(u"Downloading " + searchResult[0].name + " from " + searchResult[0].provider.name)
                self.success = search.snatchEpisode(searchResult[0])

                # give the CPU a break
                time.sleep(common.cpu_presets[sickbeard.CPU_PRESET])

            else:
                ui.notifications.message('No downloads were found',
                                         "Couldn't find a download for <i>%s</i>" % self.segment.prettyName())

                logger.log(u"Unable to find a download for: [" + self.segment.prettyName() + "]")

        except Exception:
            logger.log(traceback.format_exc(), logger.DEBUG)
        
        ### Keep a list with the 100 last executed searches
        fifo(MANUAL_SEARCH_HISTORY, self, MANUAL_SEARCH_HISTORY_SIZE)
        
        if self.success is None:
            self.success = False

        self.finish()


class BacklogQueueItem(generic_queue.QueueItem):
    def __init__(self, show, segment):
        generic_queue.QueueItem.__init__(self, 'Backlog', BACKLOG_SEARCH)
        self.priority = generic_queue.QueuePriorities.LOW
        self.name = 'BACKLOG-' + str(show.indexerid)
        self.success = None
        self.show = show
        self.segment = segment

    def run(self):
        generic_queue.QueueItem.run(self)

        try:
            logger.log("Beginning backlog search for: [" + self.show.name + "]")
            searchResult = search.searchProviders(self.show, self.segment, "wantEP", False)

            if searchResult:
                for result in searchResult:
                    # just use the first result for now
                    logger.log(u"Downloading " + result.name + " from " + result.provider.name)
                    search.snatchEpisode(result)

                    # give the CPU a break
                    time.sleep(common.cpu_presets[sickbeard.CPU_PRESET])
            else:
                logger.log(u"No needed episodes found during backlog search for: [" + self.show.name + "]")
        except Exception:
            logger.log(traceback.format_exc(), logger.DEBUG)

        if self.success is None:
            self.success = False

        self.finish()


class FailedQueueItem(generic_queue.QueueItem):
    def __init__(self, show, segment):
        generic_queue.QueueItem.__init__(self, 'Retry', FAILED_SEARCH)
        self.priority = generic_queue.QueuePriorities.HIGH
        self.name = 'RETRY-' + str(show.indexerid)
        self.show = show
        self.segment = segment
        self.success = None
        self.started = None

    def run(self):
        generic_queue.QueueItem.run(self)
        self.started = True
        
        try:
            for epObj in self.segment:
            
                logger.log(u"Marking episode as bad: [" + epObj.prettyName() + "]")
                
                failed_history.markFailed(epObj)
    
                (release, provider) = failed_history.findRelease(epObj)
                if release:
                    failed_history.logFailed(release)
                    history.logFailed(epObj, release, provider)
    
                failed_history.revertEpisode(epObj)
                logger.log("Beginning failed download search for: [" + epObj.prettyName() + "]")

            searchResult = search.searchProviders(self.show, self.segment, "wantEP", True)

            if searchResult:
                for result in searchResult:
                    # just use the first result for now
                    logger.log(u"Downloading " + result.name + " from " + result.provider.name)
                    search.snatchEpisode(result)

                    # give the CPU a break
                    time.sleep(common.cpu_presets[sickbeard.CPU_PRESET])
            else:
                pass
                #logger.log(u"No valid episode found to retry for: [" + self.segment.prettyName() + "]")
        except Exception:
            logger.log(traceback.format_exc(), logger.DEBUG)
            
        ### Keep a list with the 100 last executed searches
        fifo(MANUAL_SEARCH_HISTORY, self, MANUAL_SEARCH_HISTORY_SIZE)

        if self.success is None:
            self.success = False

        self.finish()
        
def fifo(myList, item, maxSize = 100):
    if len(myList) >= maxSize:
        myList.pop(0)
    myList.append(item)
