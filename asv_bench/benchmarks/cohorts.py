import dask
import numpy as np
import pandas as pd

import flox


class Cohorts:
    """Time the core reduction function."""

    def setup(self, *args, **kwargs):
        raise NotImplementedError

    def chunks_cohorts(self):
        return flox.core.find_group_cohorts(
            self.by,
            [self.array.chunks[ax] for ax in self.axis],
            expected_groups=self.expected,
        )

    def bitmask(self):
        chunks = [self.array.chunks[ax] for ax in self.axis]
        return flox.core._compute_label_chunk_bitmask(self.by, chunks, self.expected[-1] + 1)

    def time_find_group_cohorts(self):
        flox.core.find_group_cohorts(
            self.by,
            [self.array.chunks[ax] for ax in self.axis],
            expected_groups=self.expected,
        )
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

    def track_num_tasks_optimized(self):
        result = flox.groupby_reduce(
            self.array, self.by, func="sum", axis=self.axis, method="cohorts"
        )[0]
        (opt,) = dask.optimize(result)
        return len(opt.dask.to_dict())

    def track_num_layers(self):
        result = flox.groupby_reduce(
            self.array, self.by, func="sum", axis=self.axis, method="cohorts"
        )[0]
        return len(result.dask.layers)

    track_num_tasks.unit = "tasks"  # type: ignore[attr-defined] # Lazy
    track_num_tasks_optimized.unit = "tasks"  # type: ignore[attr-defined] # Lazy
    track_num_layers.unit = "layers"  # type: ignore[attr-defined] # Lazy
    for f in [track_num_tasks, track_num_tasks_optimized, track_num_layers]:
        f.repeat = 1  # type: ignore[attr-defined] # Lazy
        f.rounds = 1  # type: ignore[attr-defined] # Lazy
        f.number = 1  # type: ignore[attr-defined] # Lazy


class NWMMidwest(Cohorts):
    """2D labels, ireregular w.r.t chunk size.
    Mimics National Weather Model, Midwest county groupby."""

    def setup(self, *args, **kwargs):
        x = np.repeat(np.arange(30), 150)
        y = np.repeat(np.arange(30), 60)
        self.by = x[np.newaxis, :] * y[:, np.newaxis]

        self.array = dask.array.ones(self.by.shape, chunks=(350, 350))
        self.axis = (-2, -1)
        self.expected = pd.RangeIndex(self.by.max() + 1)


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
        self.by = self.time.dt.dayofyear.values - 1
        self.expected = pd.RangeIndex(self.by.max() + 1)


# class ERA5DayOfYearRechunked(ERA5DayOfYear, Cohorts):
#     def setup(self, *args, **kwargs):
#         super().setup()
#         self.array = dask.array.random.random((721, 1440, len(self.time)), chunks=(-1, -1, 24))
#         self.expected = pd.RangeIndex(self.by.max() + 1)


class ERA5MonthHour(ERA5Dataset, Cohorts):
    def setup(self, *args, **kwargs):
        super().__init__()
        by = (self.time.dt.month.values, self.time.dt.hour.values)
        ret = flox.core._factorize_multiple(
            by,
            (pd.Index(np.arange(1, 13)), pd.Index(np.arange(1, 25))),
            False,
            reindex=False,
        )
        # Add one so the rechunk code is simpler and makes sense
        self.by = ret[0][0]
        self.expected = pd.RangeIndex(self.by.max() + 1)


class ERA5MonthHourRechunked(ERA5MonthHour, Cohorts):
    def setup(self, *args, **kwargs):
        super().setup()
        super().rechunk()


class PerfectMonthly(Cohorts):
    """Perfectly chunked for a "cohorts" monthly mean climatology"""

    def setup(self, *args, **kwargs):
        self.time = pd.Series(pd.date_range("1961-01-01", "2018-12-31 23:59", freq="M"))
        self.axis = (-1,)
        self.array = dask.array.random.random((721, 1440, len(self.time)), chunks=(-1, -1, 4))
        self.by = self.time.dt.month.values - 1
        self.expected = pd.RangeIndex(self.by.max() + 1)

    def rechunk(self):
        self.array = flox.core.rechunk_for_cohorts(
            self.array, -1, self.by, force_new_chunk_at=[1], chunksize=4, ignore_old_chunks=True
        )


# class PerfectMonthlyRechunked(PerfectMonthly):
#     def setup(self, *args, **kwargs):
#         super().setup()
#         super().rechunk()


class ERA5Google(Cohorts):
    def setup(self, *args, **kwargs):
        TIME = 900  # 92044 in Google ARCO ERA5
        self.time = pd.Series(pd.date_range("1959-01-01", freq="6H", periods=TIME))
        self.axis = (2,)
        self.array = dask.array.ones((721, 1440, TIME), chunks=(-1, -1, 1))
        self.by = self.time.dt.day.values - 1
        self.expected = pd.RangeIndex(self.by.max() + 1)


def codes_for_resampling(group_as_index, freq):
    s = pd.Series(np.arange(group_as_index.size), group_as_index)
    grouped = s.groupby(pd.Grouper(freq=freq))
    first_items = grouped.first()
    counts = grouped.count()
    codes = np.repeat(np.arange(len(first_items)), counts)
    return codes


class PerfectBlockwiseResampling(Cohorts):
    """Perfectly chunked for blockwise resampling."""

    def setup(self, *args, **kwargs):
        index = pd.date_range("1959-01-01", freq="D", end="1962-12-31")
        self.time = pd.Series(index)
        TIME = len(self.time)
        self.axis = (2,)
        self.array = dask.array.ones((721, 1440, TIME), chunks=(-1, -1, 10))
        self.by = codes_for_resampling(index, freq="5D")
        self.expected = pd.RangeIndex(self.by.max() + 1)
