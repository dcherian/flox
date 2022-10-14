import dask
import numpy as np
import pandas as pd

import flox


class Cohorts:
    """Time the core reduction function."""

    def setup(self, *args, **kwargs):
        raise NotImplementedError

    def time_find_group_cohorts(self):
        flox.core.find_group_cohorts(self.by, self.array.chunks)
        # The cache clear fails dependably in CI
        # Not sure why
        try:
            flox.cache.cache.clear()
        except AttributeError:
            pass

    def time_graph_construct(self):
        flox.groupby_reduce(self.array, self.by, func="sum", axis=self.axis, method="cohorts")

    def track_num_tasks(self):
        result = flox.groupby_reduce(
            self.array, self.by, func="sum", axis=self.axis, method="cohorts"
        )[0]
        return len(result.dask.to_dict())

    track_num_tasks.unit = "tasks"


class NWMMidwest(Cohorts):
    """2D labels, ireregular w.r.t chunk size.
    Mimics National Weather Model, Midwest county groupby."""

    def setup(self, *args, **kwargs):
        x = np.repeat(np.arange(30), 150)
        y = np.repeat(np.arange(30), 60)
        self.by = x[np.newaxis, :] * y[:, np.newaxis]

        self.array = dask.array.ones(self.by.shape, chunks=(350, 350))
        self.axis = (-2, -1)


class ERA5Dataset:
    """ERA5"""

    def __init__(self, *args, **kwargs):
        self.time = pd.Series(pd.date_range("2016-01-01", "2018-12-31 23:59", freq="H"))
        self.axis = (-1,)
        self.array = dask.array.random.random((721, 1440, len(self.time)), chunks=(-1, -1, 48))

    def rechunk(self):
        self.array = flox.core.rechunk_for_cohorts(
            self.array, -1, self.by, force_new_chunk_at=[1], chunksize=48, ignore_old_chunks=True
        )


class ERA5DayOfYear(ERA5Dataset, Cohorts):
    def setup(self, *args, **kwargs):
        super().__init__()
        self.by = self.time.dt.dayofyear.values
        super().rechunk()


class Era5MonthHour(ERA5Dataset, Cohorts):
    def setup(self, *args, **kwargs):
        super().__init__()
        by = (self.time.dt.month.values, self.time.dt.hour.values)
        ret = flox.core._factorize_multiple(
            by,
            expected_groups=(pd.Index(np.arange(1, 13)), pd.Index(np.arange(1, 25))),
            by_is_dask=False,
            reindex=False,
        )
        self.by = ret[0][0]
        # self.rechunk()
