"""Bounded AI requests; callers validate/save results on the coordinating thread."""
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager


@contextmanager
def parallel_tasks(ctx, function, items, workers=2):
    items = iter(items)
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="shortsmaker-ai")
    pending = {}

    def submit():
        ctx.check()
        try:
            item = next(items)
        except StopIteration:
            return
        pending[pool.submit(function, item)] = item

    def results():
        for _ in range(workers):
            submit()
        while pending:
            ctx.check()
            done, _ = wait(pending, timeout=0.1, return_when=FIRST_COMPLETED)
            # Surface failures before scheduling any more requests.
            for future in done:
                future.result()
            for future in done:
                item = pending.pop(future)
                ctx.check()
                yield item, future.result()
                submit()

    try:
        yield results()
    except BaseException:
        ctx.abort_parallel()
        raise
    finally:
        for future in pending:
            future.cancel()
        pool.shutdown(wait=True, cancel_futures=True)
