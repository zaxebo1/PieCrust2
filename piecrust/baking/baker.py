import time
import os.path
import hashlib
import logging
from piecrust.chefutil import (
    format_timed_scope, format_timed)
from piecrust.environment import ExecutionStats
from piecrust.pipelines.base import (
    PipelineMergeRecordContext, PipelineManager,
    get_pipeline_name_for_source)
from piecrust.pipelines.records import (
    MultiRecordHistory, MultiRecord, RecordEntry,
    load_records)
from piecrust.sources.base import REALM_USER, REALM_THEME


logger = logging.getLogger(__name__)


def get_bake_records_path(app, out_dir, *, suffix=''):
    records_cache = app.cache.getCache('baker')
    records_id = hashlib.md5(out_dir.encode('utf8')).hexdigest()
    records_name = '%s%s.records' % (records_id, suffix)
    return records_cache.getCachePath(records_name)


class Baker(object):
    def __init__(self, appfactory, app, out_dir,
                 force=False, allowed_pipelines=None):
        self.appfactory = appfactory
        self.app = app
        self.out_dir = out_dir
        self.force = force

        self.allowed_pipelines = allowed_pipelines
        if allowed_pipelines is None:
            self.allowed_pipelines = list(self._pipeline_classes.keys())

    def bake(self):
        start_time = time.perf_counter()
        logger.debug("  Bake Output: %s" % self.out_dir)
        logger.debug("  Root URL: %s" % self.app.config.get('site/root'))

        # Get into bake mode.
        self.app.config.set('baker/is_baking', True)
        self.app.config.set('site/base_asset_url_format', '%uri')

        # Make sure the output directory exists.
        if not os.path.isdir(self.out_dir):
            os.makedirs(self.out_dir, 0o755)

        # Load/create the bake records.
        records_path = get_bake_records_path(
            self.app, self.out_dir)
        if not self.force and os.path.isfile(records_path):
            with format_timed_scope(logger, "loaded previous bake records",
                                    level=logging.DEBUG, colored=False):
                previous_records = load_records(records_path)
        else:
            previous_records = MultiRecord()
        current_records = MultiRecord()

        # Figure out if we need to clean the cache because important things
        # have changed.
        is_cache_valid = self._handleCacheValidity(previous_records,
                                                   current_records)
        if not is_cache_valid:
            previous_records = MultiRecord()

        # Create the bake records history which tracks what's up-to-date
        # or not since last time we baked to the given output folder.
        record_histories = MultiRecordHistory(
            previous_records, current_records)

        # Pre-create all caches.
        for cache_name in ['app', 'baker', 'pages', 'renders']:
            self.app.cache.getCache(cache_name)

        # Gather all sources by realm -- we're going to bake each realm
        # separately so we can handle "overriding" (i.e. one realm overrides
        # another realm's pages, like the user realm overriding the theme
        # realm).
        #
        # Also, create and initialize each pipeline for each source.
        has_any_pp = False
        ppmngr = PipelineManager(
            self.app, self.out_dir, record_histories)
        for source in self.app.sources:
            pname = get_pipeline_name_for_source(source)
            if pname in self.allowed_pipelines:
                ppinfo = ppmngr.createPipeline(source)
                logger.debug(
                    "Created pipeline '%s' for source: %s" %
                    (ppinfo.pipeline.PIPELINE_NAME, source.name))
                has_any_pp = True
            else:
                logger.debug(
                    "Skip source '%s' because pipeline '%s' is ignored." %
                    (source.name, pname))
        if not has_any_pp:
            raise Exception("The website has no content sources, or the bake "
                            "command was invoked with all pipelines filtered "
                            "out. There's nothing to do.")

        # Create the worker processes.
        pool_userdata = _PoolUserData(self, ppmngr, current_records)
        pool = self._createWorkerPool(records_path, pool_userdata)
        realm_list = [REALM_USER, REALM_THEME]

        # Bake the realms -- user first, theme second, so that a user item
        # can override a theme item.
        # Do this for as many times as we have pipeline passes left to do.
        pp_by_pass_and_realm = {}
        for ppinfo in ppmngr.getPipelines():
            pp_by_realm = pp_by_pass_and_realm.setdefault(
                ppinfo.pipeline.PASS_NUM, {})
            pplist = pp_by_realm.setdefault(
                ppinfo.pipeline.source.config['realm'], [])
            pplist.append(ppinfo)

        for pp_pass in sorted(pp_by_pass_and_realm.keys()):
            logger.debug("Pipelines pass %d" % pp_pass)
            pp_by_realm = pp_by_pass_and_realm[pp_pass]
            for realm in realm_list:
                pplist = pp_by_realm.get(realm)
                if pplist is not None:
                    self._bakeRealm(pool, pplist)

        # Handle deletions, collapse records, etc.
        ppmngr.buildHistoryDiffs()
        ppmngr.deleteStaleOutputs()
        ppmngr.collapseRecords()

        # All done with the workers. Close the pool and get reports.
        pool_stats = pool.close()
        total_stats = ExecutionStats()
        for ps in pool_stats:
            if ps is not None:
                total_stats.mergeStats(ps)
        current_records.stats = total_stats

        # Shutdown the pipelines.
        ppmngr.shutdownPipelines()

        # Backup previous records.
        records_dir, records_fn = os.path.split(records_path)
        records_id, _ = os.path.splitext(records_fn)
        for i in range(8, -1, -1):
            suffix = '' if i == 0 else '.%d' % i
            records_path_i = os.path.join(
                records_dir,
                '%s%s.records' % (records_id, suffix))
            if os.path.exists(records_path_i):
                records_path_next = os.path.join(
                    records_dir,
                    '%s.%s.records' % (records_id, i + 1))
                if os.path.exists(records_path_next):
                    os.remove(records_path_next)
                os.rename(records_path_i, records_path_next)

        # Save the bake records.
        with format_timed_scope(logger, "saved bake records.",
                                level=logging.DEBUG, colored=False):
            current_records.bake_time = time.time()
            current_records.out_dir = self.out_dir
            current_records.save(records_path)

        # All done.
        self.app.config.set('baker/is_baking', False)
        logger.debug(format_timed(start_time, 'done baking'))

        return current_records

    def _handleCacheValidity(self, previous_records, current_records):
        start_time = time.perf_counter()

        reason = None
        if self.force:
            reason = "ordered to"
        elif not self.app.config.get('__cache_valid'):
            # The configuration file was changed, or we're running a new
            # version of the app.
            reason = "not valid anymore"
        elif previous_records.invalidated:
            # We have no valid previous bake records.
            reason = "need bake records regeneration"
        else:
            # Check if any template has changed since the last bake. Since
            # there could be some advanced conditional logic going on, we'd
            # better just force a bake from scratch if that's the case.
            max_time = 0
            for d in self.app.templates_dirs:
                for dpath, _, filenames in os.walk(d):
                    for fn in filenames:
                        full_fn = os.path.join(dpath, fn)
                        max_time = max(max_time, os.path.getmtime(full_fn))
            if max_time >= previous_records.bake_time:
                reason = "templates modified"

        if reason is not None:
            # We have to bake everything from scratch.
            self.app.cache.clearCaches(except_names=['app', 'baker'])
            self.force = True
            current_records.incremental_count = 0
            previous_records = MultiRecord()
            logger.info(format_timed(
                start_time, "cleaned cache (reason: %s)" % reason))
            return False
        else:
            current_records.incremental_count += 1
            logger.debug(format_timed(
                start_time, "cache is assumed valid", colored=False))
            return True

    def _bakeRealm(self, pool, pplist):
        # Start with the first pass, where we iterate on the content sources'
        # items and run jobs on those.
        pool.userdata.cur_pass = 0
        next_pass_jobs = {}
        pool.userdata.next_pass_jobs = next_pass_jobs
        for ppinfo in pplist:
            src = ppinfo.source
            pp = ppinfo.pipeline

            logger.debug(
                "Queuing jobs for source '%s' using pipeline '%s' (pass 0)." %
                (src.name, pp.PIPELINE_NAME))

            next_pass_jobs[src.name] = []
            jobs = pp.createJobs()
            pool.queueJobs(jobs)
        pool.wait()

        # Now let's see if any job created a follow-up job. Let's keep
        # processing those jobs as long as they create new ones.
        pool.userdata.cur_pass = 1
        while True:
            had_any_job = False

            # Make a copy of out next pass jobs and reset the list, so
            # the first jobs to be processed don't mess it up as we're
            # still iterating on it.
            next_pass_jobs = pool.userdata.next_pass_jobs
            pool.userdata.next_pass_jobs = {}

            for sn, jobs in next_pass_jobs.items():
                if jobs:
                    logger.debug(
                        "Queuing jobs for source '%s' (pass %d)." %
                        (sn, pool.userdata.cur_pass))
                    pool.userdata.next_pass_jobs[sn] = []
                    pool.queueJobs(jobs)
                    had_any_job = True

            if not had_any_job:
                break

            pool.wait()
            pool.userdata.cur_pass += 1

    def _logErrors(self, item_spec, errors):
        logger.error("Errors found in %s:" % item_spec)
        for e in errors:
            logger.error("  " + e)

    def _createWorkerPool(self, previous_records_path, pool_userdata):
        from piecrust.workerpool import WorkerPool
        from piecrust.baking.worker import BakeWorkerContext, BakeWorker

        worker_count = self.app.config.get('baker/workers')
        batch_size = self.app.config.get('baker/batch_size')

        ctx = BakeWorkerContext(
            self.appfactory,
            self.out_dir,
            force=self.force,
            previous_records_path=previous_records_path,
            allowed_pipelines=self.allowed_pipelines)
        pool = WorkerPool(
            worker_count=worker_count,
            batch_size=batch_size,
            worker_class=BakeWorker,
            initargs=(ctx,),
            callback=self._handleWorkerResult,
            error_callback=self._handleWorkerError,
            userdata=pool_userdata)
        return pool

    def _handleWorkerResult(self, job, res, userdata):
        cur_pass = userdata.cur_pass
        record = userdata.records.getRecord(job.record_name)

        if cur_pass == 0:
            record.addEntry(res.record_entry)
        else:
            ppinfo = userdata.ppmngr.getPipeline(job.source_name)
            ppmrctx = PipelineMergeRecordContext(
                record, job, cur_pass)
            ppinfo.pipeline.mergeRecordEntry(res.record_entry, ppmrctx)

        npj = res.next_pass_job
        if npj is not None:
            npj.data['pass'] = cur_pass + 1
            userdata.next_pass_jobs[job.source_name].append(npj)

        if not res.record_entry.success:
            record.success = False
            userdata.records.success = False
            self._logErrors(job.content_item.spec, res.record_entry.errors)

    def _handleWorkerError(self, job, exc_data, userdata):
        cur_pass = userdata.cur_pass
        record = userdata.records.getRecord(job.record_name)

        if cur_pass == 0:
            ppinfo = userdata.ppmngr.getPipeline(job.source_name)
            entry_class = ppinfo.pipeline.RECORD_ENTRY_CLASS or RecordEntry
            e = entry_class()
            e.item_spec = job.content_item.spec
            e.errors.append(str(exc_data))
            record.addEntry(e)
        else:
            e = record.getEntry(job.content_item.spec)
            e.errors.append(str(exc_data))

        record.success = False
        userdata.records.success = False

        self._logErrors(job.content_item.spec, e.errors)
        if self.app.debug:
            logger.error(exc_data.traceback)


class _PoolUserData:
    def __init__(self, baker, ppmngr, current_records):
        self.baker = baker
        self.ppmngr = ppmngr
        self.records = current_records
        self.cur_pass = 0
        self.next_pass_jobs = {}
