# Copyright (C) 2012 Science and Technology Facilities Council.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

from __future__ import print_function

import datetime
import pytz
from threading import Lock
from traceback import extract_stack

from sqlite3 import DatabaseError

from crab import CrabError, CrabStatus
from crab.store import CrabStore

class CrabDBLock():
    def __init__(self, conn):
        self.lock = Lock()
        self.conn = conn
        self.laststack = ''

    def __enter__(self):
        try:
            st = extract_stack(limit=4)
            del st[-1]
            stack = ' '.join([fn for (fi, ln, fn, l) in st])
        except:
            stack = ''

        if not self.lock.acquire(False):
            print('Blocking:', stack, '--', self.laststack)
            self.lock.acquire(True)

        # No 'begin transaction' function for SQLite.

        self.laststack = stack

    def __exit__(self, type, value, tb):
        if type is None:
            self.conn.commit()
        else:
            self.conn.rollback()

        self.laststack = ''
        self.lock.release()

class CrabDB(CrabStore):
    """Crab storage backend using a database.

    Currently written for SQLite but since it uses the Python DB API
    it should be possible to generalize it by altering the queries
    based on the database type where necessary."""

    def __init__(self, conn, outputstore=None):
        """Constructor for CrabDB.

        Records the reference to the database connection for future reference.

        A separate storage backend can be provided for the storage of
        job output.  An outputstore should implement write_job_output
        and get_job_output, and if provided will be used instead of
        writing the stdout and stderr from the cron jobs to the database.
        The outputstore should only raise instances of CrabError."""

        self.conn = conn
        self.outputstore = outputstore

        self.lock = CrabDBLock(conn)

    def get_jobs(self, host=None, user=None, include_deleted=False):
        """Fetches a list of all of the cron jobs stored in the database,
        excluding deleted jobs by default.

        Optionally filters by host or username if these parameters are
        supplied."""

        params = []
        conditions = []

        if not include_deleted:
            conditions.append('deleted IS NULL')

        if host is not None:
            conditions.append('host=?')
            params.append(host)

        if user is not None:
            conditions.append('user=?')
            params.append(user)

        if conditions:
            where_clause = 'WHERE ' + ' AND '.join(conditions)
        else:
            where_clause = ''

        with self.lock:
            return self._query_to_dict_list(
                'SELECT id, host, user, jobid, command, time, ' +
                    'timezone, installed, deleted ' +
                'FROM job ' + where_clause + ' ' +
                'ORDER BY host ASC, user ASC, installed ASC', params)

    # TODO: decide if this function is in its most sensible form.
    # This version added while extracting the crontab importing code
    # from the database module.
    def get_user_job_set(self, host, user):
        """Prepares a set of job ID numbers for the given host and user
        including deleted jobs."""

        with self.lock:
            c = self.conn.cursor()
            id_ = set()

            try:
                c.execute('SELECT id FROM job WHERE host=? AND user=?',
                          [host, user])
                while True:
                    row = c.fetchone()
                    if row is None:
                        break
                    id_.add(row[0])
            except DatabaseError as err:
                raise CrabError('database error : ' + str(err))

            finally:
                c.close()

            return id_

    def _check_job(self, host, user, jobid, command,
                  time=None, timezone=None):
        """Ensure that a job exists in the database.

        Tries to find (and update if necessary) the corresponding job in
        the database.  If it is not found, the job is stored as a new
        entry.

        In either case, the job's ID number is returned.

        This is a private method because it must be run within
        a database transaction."""

        c = self.conn.cursor()

        try:

            # We know the jobid, so use it to search

            if jobid is not None:
                c.execute('SELECT id, command, time, timezone, deleted '
                          'FROM job WHERE host=? AND user=? AND jobid=?',
                          [host, user, jobid])
                row = c.fetchone()

                if row is not None:
                    (id_, dbcommand, dbtime, dbtz, deleted) = row

                    if (deleted is None and
                            command == dbcommand and
                            (time is None or time == dbtime) and
                            (timezone is None or timezone == dbtz)):
                        pass

                    else:
                        if time is None:
                            time = dbtime
                        if timezone is None:
                            timezone = dbtz

                        c.execute('UPDATE job SET command=?, time=?, '
                                  'timezone=?, installed=CURRENT_TIMESTAMP, '
                                  'deleted=NULL WHERE id=?',
                                  [command, time, timezone, id_])

                    return id_

                else:
                    # Need to check if the job already existed without
                    # a job ID, in which case we update it to add the job ID.

                    c.execute('SELECT id, time, timezone, deleted '
                              'FROM job WHERE host=? AND user=? AND command=? '
                              'AND jobid IS NULL',
                              [host, user, command])

                    row = c.fetchone()

                    if row is not None:
                        (id_, dbtime, dbtz, deleted) = row

                        if time is None:
                            time = dbtime
                        if timezone is None:
                            timezone = dbtz

                        c.execute('UPDATE job SET time=?, timezone=?, '
                                  'jobid=?, '
                                  'installed=CURRENT_TIMESTAMP, deleted=NULL '
                                  'WHERE id=?',
                                  [time, timezone, jobid, id_])

                        return id_

                    else:
                      return self._insert_job(host, user, jobid, time,
                                              command, timezone)

            # We don't know the jobid, so we must search by command.
            # In general we can't distinguish multiple copies of the same
            # command running at different times.
            # Such jobs should be given job IDs, or combined using
            # time ranges / steps.

            else:
                c.execute('SELECT id, time, timezone, deleted '
                          'FROM job WHERE host=? AND user=? AND command=?',
                          [host, user, command])

                row = c.fetchone()

                if row is not None:
                    (id_, dbtime, dbtz, deleted) = row

                    if (deleted is None and
                            (time is None or time == dbtime) and
                            (timezone is None or timezone == dbtz)):
                        pass

                    else:
                        if time is None:
                            time = dbtime
                        if timezone is None:
                            timezone = dbtz

                        c.execute('UPDATE job SET time=?, timezone=?, '
                                  'installed=CURRENT_TIMESTAMP, deleted=NULL '
                                  'WHERE id=?',
                                  [time, timezone, id_])

                    return id_

                else:
                    return self._insert_job(host, user, jobid,
                                            time, command, timezone)

        except DatabaseError as err:
            raise CrabError('database error : ' + str(err))

        finally:
            c.close()

    def _insert_job(self, host, user, jobid, time, command, timezone):
        """Inserts a job record into the database."""

        c = self.conn.cursor()

        try:
            c.execute('INSERT INTO job (host, user, jobid, ' +
                          'time, command, timezone)' +
                          'VALUES (?, ?, ?, ?, ?, ?)',
                      [host, user, jobid, time, command, timezone])

            return c.lastrowid

        except DatabaseError as err:
            raise CrabError('database error : ' + str(err))

        finally:
            c.close()

    def _delete_job(self, id_):
        """Marks a job as deleted in the database."""

        c = self.conn.cursor()

        try:
            c.execute('UPDATE job SET deleted=CURRENT_TIMESTAMP ' +
                      'WHERE id=?',
                      [id_])

        except DatabaseError as err:
            raise CrabError('database error : ' + str(err))

        finally:
            c.close()

    def log_start(self, host, user, jobid, command):
        """Inserts a job start record into the database."""

        with self.lock:
            id_ = self._check_job(host, user, jobid, command)

            c = self.conn.cursor()

            try:
                c.execute('INSERT INTO jobstart (jobid, command) VALUES (?, ?)',
                          [id_, command])

            except DatabaseError as err:
                raise CrabError('database error : ' + str(err))

            finally:
                c.close()

    def log_finish(self, host, user, jobid, command, status,
                   stdout=None, stderr=None):
        """Inserts a job finish record into the database.

        The output will be passed to the write_job_output method."""

        with self.lock:
            c = self.conn.cursor()

            try:
                id_ = self._check_job(host, user, jobid, command)

                c.execute('INSERT INTO jobfinish (jobid, command, status) ' +
                          'VALUES (?, ?, ?)',
                          [id_, command, status])

                finishid = c.lastrowid

            except DatabaseError as err:
                raise CrabError('database error : ' + str(err))

            finally:
                c.close()

        self.write_job_output(finishid, host, user, id_, stdout, stderr)

    def log_warning(self, id_, status):
        """Inserts a warning regarding a job into the database.

        This is for warnings generated interally by crab, for example
        from the monitor thread.  Such warnings are currently stored
        in an separate table and do not have any associated output
        records."""

        with self.lock:
            c = self.conn.cursor()

            try:
                c.execute('INSERT INTO jobwarn (jobid, status) VALUES (?, ?)',
                          [id_, status])

            except DatabaseError as err:
                raise CrabError('database error : ' + str(err))

            finally:
                c.close()

    def get_job_info(self, id_):
        """Retrieve information about a job by ID number."""

        with self.lock:
            return self._query_to_dict(
                'SELECT host, user, command, jobid, time, timezone, ' +
                    'installed, deleted ' +
                    'FROM job WHERE id = ?', [id_])

    def get_job_config(self, id_):
        """Retrieve configuration data for a job by ID number."""

        with self.lock:
            return self._query_to_dict(
                'SELECT graceperiod, timeout ' +
                'FROM jobconfig WHERE jobid = ?', [id_])

    def write_job_config(self, id_, graceperiod=None, timeout=None):
        """Writes configuration data for a job by ID number."""

        with self.lock:
            row = self._query_to_dict('SELECT id as configid '
                                      'FROM jobconfig '
                                      'WHERE jobid = ?', [id_])

            c = self.conn.cursor()

            try:
                if row is None:
                    c.execute('INSERT INTO jobconfig (jobid, graceperiod, '
                              'timeout) VALUES (?, ?, ?)',
                              [id_, graceperiod, timeout])
                else:
                    configid = row['configid']
                    if configid is None:
                        raise CrabError('job config: got null id')

                    c.execute('UPDATE jobconfig SET graceperiod=?, timeout=? '
                              'WHERE id=?', [graceperiod, timeout, configid])

            except DatabaseError as err:
                raise CrabError('database error: ' + str(err))

            finally:
                c.close()

    def get_orphan_configs(self):
        """Make a list of orphaned job configuration records."""

        with self.lock:
            return self._query_to_dict_list(
                'SELECT jobconfig.id AS configid, job.id AS id, '
                    'host, user, job.jobid AS jobid, command '
                'FROM jobconfig JOIN job ON jobconfig.jobid = job.id '
                'WHERE job.deleted IS NOT NULL')

    def relink_job_config(self, configid, id_):
        with self.lock:
            c = self.conn.cursor()

            try:
                c.execute('UPDATE jobconfig SET jobid = ? '
                          'WHERE id = ?', [id_, configid])

            except DatabseError as err:
                raise CrabError('database error: ' + str(err))

            finally:
                c.close()

    def get_job_finishes(self, id_, limit=100):
        """Retrieves a list of recent job finish events for the given job,
        most recent first."""

        with self.lock:
            return self._query_to_dict_list(
                'SELECT id, datetime, command, status ' +
                    'FROM jobfinish WHERE jobid = ? ' +
                    'ORDER BY datetime DESC LIMIT ?',
                [id_, limit])

    def get_job_events(self, id_, limit=100, start=None, end=None):
        """Fetches a combined list of events relating to the specified job.

        Return events, newest first (with finishes first for the same
        datetime).  This ordering allows us to apply the SQL limit on
        number of result rows to find the most recent events.  It gives
        the correct ordering for the job info page."""

        # TODO: implement start and end as WHERE clause.
        conditions = ['jobid=?']
        params = [id_]

        if start is not None:
            conditions.append('datetime>=?')
            params.append(self.format_datetime(start))

        if end is not None:
            conditions.append('datetime<?')
            params.append(self.format_datetime(end))

        where_clause = 'WHERE ' + ' AND '.join(conditions)
        params = params * 3

        if limit is None:
            limit_clause = ''
        else:
            limit_clause = 'LIMIT ?'
            params.append(limit)

        with self.lock:
            return self._query_to_dict_list(
                'SELECT ' +
                    'id, 1 AS type, ' +
                    'datetime, command, NULL AS status FROM jobstart ' +
                        where_clause + ' ' +
                'UNION SELECT ' +
                    'id, 2 AS type, ' +
                        'datetime, NULL AS command, status FROM jobwarn ' +
                        where_clause + ' ' +
                'UNION SELECT ' +
                    'id, 3 AS type, ' +
                        'datetime, command, status FROM jobfinish ' +
                        where_clause + ' ' +
                'ORDER BY datetime DESC, type DESC ' + limit_clause,
                params)

    def get_events_since(self, startid, warnid, finishid):
        """Extract minimal summary information for events on all jobs
        since the given IDs, oldest first."""

        with self.lock:
            return self._query_to_dict_list(
                'SELECT ' +
                    'jobid, id, 1 AS type, ' +
                    'datetime, NULL AS status FROM jobstart ' +
                    'WHERE id > ? ' +
                'UNION SELECT ' +
                    'jobid, id, 2 AS type, ' +
                    'datetime, status FROM jobwarn ' +
                    'WHERE id > ? ' +
                'UNION SELECT ' +
                    'jobid, id, 3 AS type, ' +
                    'datetime, status FROM jobfinish ' +
                    'WHERE id > ? ' +
                'ORDER BY datetime ASC, type ASC',
                [startid, warnid, finishid])

    def get_fail_events(self, limit=40):
        """Retrieves the most recent failures for all events,
        combining the finish and warning tables.

        This method has to include a list of status codes to exclude
        since the filtering is done in the SQL.  The codes skipped
        are LATE and SUCCESS."""

        with self.lock:
            return self._query_to_dict_list(
                'SELECT ' +
                    'job.id AS id, status, datetime, host, user, ' +
                    'job.jobid AS jobid, jobfinish.command AS command, ' +
                    'jobfinish.id AS finishid ' +
                    'FROM jobfinish JOIN job ON jobfinish.jobid = job.id ' +
                    'WHERE status NOT IN (?, ?) ' +
                'UNION SELECT ' +
                    'job.id AS id, status, datetime, host, user, ' +
                    'job.jobid AS jobid, job.command AS command, ' +
                    'NULL as finishid ' +
                    'FROM jobwarn JOIN job ON jobwarn.jobid = job.id ' +
                    'WHERE status NOT IN (?, ?) ' +
                'ORDER BY datetime DESC, status DESC LIMIT ?',
                [CrabStatus.SUCCESS, CrabStatus.LATE,
                 CrabStatus.SUCCESS, CrabStatus.LATE, limit])

    def _query_to_dict(self, sql, param=[]):
        """Convenience method which returns a single row from
        _query_to_dict_list.

        Returns None if the result does not have exactly one row."""

        result = self._query_to_dict_list(sql, param)
        if len(result) == 1:
            return result[0]
        else:
            return None

    def _query_to_dict_list(self, sql, param=[]):
        """Execute an SQL query and return the result as a list of
        Python dict objects.

        The dict keys are retrieved from the SQL result using the
        description method of the DB cursor object."""

        c = self.conn.cursor()
        output = []

        try:
            c.execute(sql, param)

            while True:
                row = c.fetchone()
                if row is None:
                    break

                dict = {}

                for (i, coldescription) in enumerate(c.description):
                    dict[coldescription[0]] = row[i]

                output.append(dict)

        except DatabaseError as err:
            raise CrabError('database error : ' + str(err))

        finally:
            c.close()

        return output

    def write_job_output(self, finishid, host, user, id_,
                         stdout, stderr):
        """Writes the job output to the database.

        This method does not require the host, user, or job ID
        number, but will pass them to the outputstore's corresponding
        method if it is defined rather than performing this action
        with the database."""

        if self.outputstore is not None:
            return self.outputstore.write_job_output(finishid, host, user, id_,
                                                     stdout, stderr)

        with self.lock:
            c = self.conn.cursor()

            try:
                c.execute('INSERT INTO joboutput (finishid, stdout, stderr) ' +
                          'VALUES (?, ?, ?)',
                          [finishid, stdout, stderr])

            except DatabaseError as err:
                raise CrabError('database error : ' + str(err))

            finally:
                c.close()

    def get_job_output(self, finishid, host, user, id_):
        """Fetches the standard output and standard error for the
        given finish ID.

        The result is returned as a two element list.  The parameters
        host, user and id number are passed on to the outputstore's
        get_job_output method if an outputstore was provided to the
        contructor, allowing the outputstore to organise its
        information hierarchically if desired.  Otherwise this method
        does not make use of those parameters.  Returns None if no
        output is found."""

        if self.outputstore is not None:
            return self.outputstore.get_job_output(finishid, host, user, id_)

        with self.lock:
            c = self.conn.cursor()

            try:
                c.execute('SELECT stdout, stderr FROM joboutput ' +
                          'WHERE finishid=?', [finishid])

                row = c.fetchone()

                if row is None:
                    return None

                return row

            except DatabaseError as err:
                raise CrabError('database error : ' + str(err))

            finally:
                c.close()

    def parse_datetime(self, timestamp):
        """Parses the timestamp strings used by the database.

        This is a method in this class so that it could potentially
        adapt to different databases.

        The returned datetime object will include the correct timezone:
        for SQLite this is always UTC.

        An alternative thing to do would be to have _query_to_dict_list
        guess which fields are timestamps and automatically run this
        method on them."""

        return datetime.datetime.strptime(timestamp,
                        '%Y-%m-%d %H:%M:%S').replace(tzinfo=pytz.UTC)

    def format_datetime(self, datetime_):
        """Converts a datetime into a timestamp string for the database.

        Includes conversion to UTC as used by SQLite."""

        return datetime_.astimezone(pytz.UTC).strftime('%Y-%m-%d %H:%M:%S')

    def write_raw_crontab(self, host, user, crontab):
        if self.outputstore is not None and hasattr(self.outputstore,
                                                    'write_raw_crontab'):
            return self.outputstore.write_raw_crontab(host, user, crontab)

        with self.lock:
            entry = self._query_to_dict('SELECT id FROM rawcrontab ' +
                                        'WHERE host = ? AND user = ?',
                                        [host, user])

            c = self.conn.cursor()

            try:
                if entry is None:
                    c.execute('INSERT INTO rawcrontab (host, user, crontab) '
                              'VALUES (?, ?, ?)',
                              [host, user, '\n'.join(crontab)])
                else:
                    c.execute('UPDATE rawcrontab SET crontab = ? WHERE id = ?',
                              ['\n'.join(crontab), entry['id']])

            except DatabaseError as err:
                raise CrabError('database error : ' + str(err))

            finally:
                c.close()

    def get_raw_crontab(self, host, user):
        if self.outputstore is not None and hasattr(self.outputstore,
                                                    'get_raw_crontab'):
            return self.outputstore.get_raw_crontab(host, user)

        with self.lock:
            entry = self._query_to_dict('SELECT crontab FROM rawcrontab ' +
                                        'WHERE host = ? AND user = ?',
                                        [host, user])

        if entry is None:
            return None
        else:
            return entry['crontab'].split('\n')

    def get_notifications(self):
        """Fetches a list of notifications, combining those defined
        by a config ID with those defined by user and/or host."""

        with self.lock:
            return self._query_to_dict_list(
                'SELECT jobnotify.id AS notifyid, method, address, '
                        'skip_ok, skip_warning, skip_error, include_output, '
                        'jobconfig.jobid AS id, jobnotify.time AS time, '
                        'COALESCE(jobnotify.timezone, job.timezone) '
                            'AS timezone '
                    'FROM jobnotify '
                        'JOIN jobconfig '
                            'ON jobnotify.configid = jobconfig.id '
                        'JOIN job '
                            'ON job.id = jobconfig.jobid '
                    'WHERE configid IS NOT NULL AND job.deleted IS NULL '
                'UNION SELECT jobnotify.id AS notifyid, method, address, '
                        'skip_ok, skip_warning, skip_error, include_output, '
                        'job.id AS id, jobnotify.time AS time, '
                        'COALESCE(jobnotify.timezone, job.timezone) '
                            'AS timezone '
                    'FROM jobnotify JOIN job '
                        'ON COALESCE(job.user = jobnotify.user, 1) '
                        'AND COALESCE(job.host = jobnotify.host, 1) '
                    'WHERE configid IS NULL AND job.deleted IS NULL',
                [])
