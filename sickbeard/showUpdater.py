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
#  GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Sick Beard.  If not, see <http://www.gnu.org/licenses/>.

import datetime
import os

import sickbeard

from sickbeard import logger
from sickbeard import exceptions
from sickbeard import ui
from sickbeard.exceptions import ex
from sickbeard import encodingKludge as ek
from sickbeard import db
from sickbeard import network_timezones
from sickbeard import failed_history

class ShowUpdater():

    def run(self, force=False):

        update_datetime = datetime.datetime.now()
        update_date = update_datetime.date()

        # refresh network timezones
        network_timezones.update_network_dict()

        # sure, why not?
        if sickbeard.USE_FAILED_DOWNLOADS:
            failed_history.trimHistory()

        logger.log(u"Doing full update on all shows")

        # clean out cache directory, remove everything > 12 hours old
        sickbeard.helpers.clearCache()

        # select 10 'Ended' tv_shows updated more than 90 days ago to include in this update
        stale_should_update = []
        stale_update_date = (update_date - datetime.timedelta(days=90)).toordinal()

        # last_update_date <= 90 days, sorted ASC because dates are ordinal
        myDB = db.DBConnection()
        sql_result = myDB.select(
            "SELECT indexer_id FROM tv_shows WHERE status = 'Ended' AND last_update_indexer <= ? ORDER BY last_update_indexer ASC LIMIT 10;",
            [stale_update_date])

        for cur_result in sql_result:
            stale_should_update.append(int(cur_result['indexer_id']))

        # start update process
        piList = []
        for curShow in sickbeard.showList:

            try:
                # get next episode airdate
                curShow.nextEpisode()

                # if should_update returns True (not 'Ended') or show is selected stale 'Ended' then update, otherwise just refresh
                if curShow.should_update(update_date=update_date) or curShow.indexerid in stale_should_update:
                    curQueueItem = sickbeard.showQueueScheduler.action.updateShow(curShow, True)  # @UndefinedVariable
                else:
                    logger.log(
                        u"Not updating episodes for show " + curShow.name + " because it's marked as ended and last/next episode is not within the grace period.",
                        logger.DEBUG)
                    curQueueItem = sickbeard.showQueueScheduler.action.refreshShow(curShow, True)  # @UndefinedVariable

                piList.append(curQueueItem)

            except (exceptions.CantUpdateException, exceptions.CantRefreshException), e:
                logger.log(u"Automatic update failed: " + ex(e), logger.ERROR)

        ui.ProgressIndicators.setIndicator('dailyUpdate', ui.QueueProgressIndicator("Daily Update", piList))

        logger.log(u"Completed full update on all shows")

    def __del__(self):
        pass
