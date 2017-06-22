#!/usr/bin/env python2.7
""" 
    Utility for converting *_clat_hist* files generated by fio into latency statistics.
    
    Example usage:
    
            $ fiologparser_hist.py *_clat_hist*
            end-time, samples, min, avg, median, 90%, 95%, 99%, max
            1000, 15, 192, 1678.107, 1788.859, 1856.076, 1880.040, 1899.208, 1888.000
            2000, 43, 152, 1642.368, 1714.099, 1816.659, 1845.552, 1888.131, 1888.000
            4000, 39, 1152, 1546.962, 1545.785, 1627.192, 1640.019, 1691.204, 1744
            ...
    
    @author Karl Cronburg <karl.cronburg@gmail.com>
"""
import os
import sys
import pandas
import numpy as np

err = sys.stderr.write

def weighted_percentile(percs, vs, ws):
    """ Use linear interpolation to calculate the weighted percentile.
        
        Value and weight arrays are first sorted by value. The cumulative
        distribution function (cdf) is then computed, after which np.interp
        finds the two values closest to our desired weighted percentile(s)
        and linearly interpolates them.
        
        percs  :: List of percentiles we want to calculate
        vs     :: Array of values we are computing the percentile of
        ws     :: Array of weights for our corresponding values
        return :: Array of percentiles
    """
    idx = np.argsort(vs)
    vs, ws = vs[idx], ws[idx] # weights and values sorted by value
    cdf = 100 * (ws.cumsum() - ws / 2.0) / ws.sum()
    return np.interp(percs, cdf, vs) # linear interpolation

def weights(start_ts, end_ts, start, end):
    """ Calculate weights based on fraction of sample falling in the
        given interval [start,end]. Weights computed using vector / array
        computation instead of for-loops.
    
        Note that samples with zero time length are effectively ignored
        (we set their weight to zero).

        start_ts :: Array of start times for a set of samples
        end_ts   :: Array of end times for a set of samples
        start    :: int
        end      :: int
        return   :: Array of weights
    """
    sbounds = np.maximum(start_ts, start).astype(float)
    ebounds = np.minimum(end_ts,   end).astype(float)
    ws = (ebounds - sbounds) / (end_ts - start_ts)
    if np.any(np.isnan(ws)):
      err("WARNING: zero-length sample(s) detected. Log file corrupt"
          " / bad time values? Ignoring these samples.\n")
    ws[np.where(np.isnan(ws))] = 0.0;
    return ws

def weighted_average(vs, ws):
    return np.sum(vs * ws) / np.sum(ws)

columns = ["end-time", "samples", "min", "avg", "median", "90%", "95%", "99%", "max"]
percs   = [50, 90, 95, 99]

def fmt_float_list(ctx, num=1):
  """ Return a comma separated list of float formatters to the required number
      of decimal places. For instance:

        fmt_float_list(ctx.decimals=4, num=3) == "%.4f, %.4f, %.4f"
  """
  return ', '.join(["%%.%df" % ctx.decimals] * num)

# Default values - see beginning of main() for how we detect number columns in
# the input files:
__HIST_COLUMNS = 1216
__NON_HIST_COLUMNS = 3
__TOTAL_COLUMNS = __HIST_COLUMNS + __NON_HIST_COLUMNS
    
def read_chunk(rdr, sz):
    """ Read the next chunk of size sz from the given reader. """
    try:
        """ StopIteration occurs when the pandas reader is empty, and AttributeError
            occurs if rdr is None due to the file being empty. """
        new_arr = rdr.read().values
    except (StopIteration, AttributeError):
        return None    

    """ Extract array of just the times, and histograms matrix without times column. """
    times, rws, szs = new_arr[:,0], new_arr[:,1], new_arr[:,2]
    hists = new_arr[:,__NON_HIST_COLUMNS:]
    times = times.reshape((len(times),1))
    arr = np.append(times, hists, axis=1)

    return arr

def get_min(fps, arrs):
    """ Find the file with the current first row with the smallest start time """
    return min([fp for fp in fps if not arrs[fp] is None], key=lambda fp: arrs.get(fp)[0][0])

def histogram_generator(ctx, fps, sz):
    
    # Create a chunked pandas reader for each of the files:
    rdrs = {}
    for fp in fps:
        try:
            rdrs[fp] = pandas.read_csv(fp, dtype=int, header=None, chunksize=sz)
        except ValueError as e:
            if e.message == 'No columns to parse from file':
                if ctx.warn: sys.stderr.write("WARNING: Empty input file encountered.\n")
                rdrs[fp] = None
            else:
                raise(e)

    # Initial histograms from disk:
    arrs = {fp: read_chunk(rdr, sz) for fp,rdr in rdrs.items()}
    while True:

        try:
            """ ValueError occurs when nothing more to read """
            fp = get_min(fps, arrs)
        except ValueError:
            return
        arr = arrs[fp]
        yield np.insert(arr[0], 1, fps.index(fp))
        arrs[fp] = arr[1:]

        if arrs[fp].shape[0] == 0:
            arrs[fp] = read_chunk(rdrs[fp], sz)

def _plat_idx_to_val(idx, edge=0.5, FIO_IO_U_PLAT_BITS=6, FIO_IO_U_PLAT_VAL=64):
    """ Taken from fio's stat.c for calculating the latency value of a bin
        from that bin's index.
        
            idx  : the value of the index into the histogram bins
            edge : fractional value in the range [0,1]** indicating how far into
            the bin we wish to compute the latency value of.
        
        ** edge = 0.0 and 1.0 computes the lower and upper latency bounds
           respectively of the given bin index. """

    # MSB <= (FIO_IO_U_PLAT_BITS-1), cannot be rounded off. Use
    # all bits of the sample as index
    if (idx < (FIO_IO_U_PLAT_VAL << 1)):
        return idx 

    # Find the group and compute the minimum value of that group
    error_bits = (idx >> FIO_IO_U_PLAT_BITS) - 1 
    base = 1 << (error_bits + FIO_IO_U_PLAT_BITS)

    # Find its bucket number of the group
    k = idx % FIO_IO_U_PLAT_VAL

    # Return the mean (if edge=0.5) of the range of the bucket
    return base + ((k + edge) * (1 << error_bits))
    
def plat_idx_to_val_coarse(idx, coarseness, edge=0.5):
    """ Converts the given *coarse* index into a non-coarse index as used by fio
        in stat.h:plat_idx_to_val(), subsequently computing the appropriate
        latency value for that bin.
        """

    # Multiply the index by the power of 2 coarseness to get the bin
    # bin index with a max of 1536 bins (FIO_IO_U_PLAT_GROUP_NR = 24 in stat.h)
    stride = 1 << coarseness
    idx = idx * stride
    lower = _plat_idx_to_val(idx, edge=0.0)
    upper = _plat_idx_to_val(idx + stride, edge=1.0)
    return lower + (upper - lower) * edge

def print_all_stats(ctx, end, mn, ss_cnt, vs, ws, mx):
    ps = weighted_percentile(percs, vs, ws)

    avg = weighted_average(vs, ws)
    values = [mn, avg] + list(ps) + [mx]
    row = [end, ss_cnt] + map(lambda x: float(x) / ctx.divisor, values)
    fmt = "%d, %d, %d, " + fmt_float_list(ctx, 5) + ", %d"
    print (fmt % tuple(row))

def update_extreme(val, fncn, new_val):
    """ Calculate min / max in the presence of None values """
    if val is None: return new_val
    else: return fncn(val, new_val)

# See beginning of main() for how bin_vals are computed
bin_vals = []
lower_bin_vals = [] # lower edge of each bin
upper_bin_vals = [] # upper edge of each bin 

def process_interval(ctx, samples, iStart, iEnd):
    """ Construct the weighted histogram for the given interval by scanning
        through all the histograms and figuring out which of their bins have
        samples with latencies which overlap with the given interval
        [iStart,iEnd].
    """
    
    times, files, hists = samples[:,0], samples[:,1], samples[:,2:]
    iHist = np.zeros(__HIST_COLUMNS)
    ss_cnt = 0 # number of samples affecting this interval
    mn_bin_val, mx_bin_val = None, None

    for end_time,file,hist in zip(times,files,hists):
            
        # Only look at bins of the current histogram sample which
        # started before the end of the current time interval [start,end]
        start_times = (end_time - 0.5 * ctx.interval) - bin_vals / 1000.0
        idx = np.where(start_times < iEnd)
        s_ts, l_bvs, u_bvs, hs = start_times[idx], lower_bin_vals[idx], upper_bin_vals[idx], hist[idx]

        # Increment current interval histogram by weighted values of future histogram:
        ws = hs * weights(s_ts, end_time, iStart, iEnd)
        iHist[idx] += ws
    
        # Update total number of samples affecting current interval histogram:
        ss_cnt += np.sum(hs)
        
        # Update min and max bin values seen if necessary:
        idx = np.where(hs != 0)[0]
        if idx.size > 0:
            mn_bin_val = update_extreme(mn_bin_val, min, l_bvs[max(0,           idx[0]  - 1)])
            mx_bin_val = update_extreme(mx_bin_val, max, u_bvs[min(len(hs) - 1, idx[-1] + 1)])

    if ss_cnt > 0: print_all_stats(ctx, iEnd, mn_bin_val, ss_cnt, bin_vals, iHist, mx_bin_val)

def guess_max_from_bins(ctx, hist_cols):
    """ Try to guess the GROUP_NR from given # of histogram
        columns seen in an input file """
    max_coarse = 8
    if ctx.group_nr < 19 or ctx.group_nr > 26:
        bins = [ctx.group_nr * (1 << 6)]
    else:
        bins = [1216,1280,1344,1408,1472,1536,1600,1664]
    coarses = range(max_coarse + 1)
    fncn = lambda z: list(map(lambda x: z/2**x if z % 2**x == 0 else -10, coarses))
    
    arr = np.transpose(list(map(fncn, bins)))
    idx = np.where(arr == hist_cols)
    if len(idx[1]) == 0:
        table = repr(arr.astype(int)).replace('-10', 'N/A').replace('array','     ')
        err("Unable to determine bin values from input clat_hist files. Namely \n"
            "the first line of file '%s' " % ctx.FILE[0] + "has %d \n" % (__TOTAL_COLUMNS,) +
            "columns of which we assume %d " % (hist_cols,) + "correspond to histogram bins. \n"
            "This number needs to be equal to one of the following numbers:\n\n"
            + table + "\n\n"
            "Possible reasons and corresponding solutions:\n"
            "  - Input file(s) does not contain histograms.\n"
            "  - You recompiled fio with a different GROUP_NR. If so please specify this\n"
            "    new GROUP_NR on the command line with --group_nr\n")
        exit(1)
    return bins[idx[1][0]]

def main(ctx):

    if ctx.job_file:
        try:
            from configparser import SafeConfigParser, NoOptionError
        except ImportError:
            from ConfigParser import SafeConfigParser, NoOptionError

        cp = SafeConfigParser(allow_no_value=True)
        with open(ctx.job_file, 'r') as fp:
            cp.readfp(fp)

        if ctx.interval is None:
            # Auto detect --interval value
            for s in cp.sections():
                try:
                    hist_msec = cp.get(s, 'log_hist_msec')
                    if hist_msec is not None:
                        ctx.interval = int(hist_msec)
                except NoOptionError:
                    pass

    if ctx.interval is None:
        ctx.interval = 1000

    # Automatically detect how many columns are in the input files,
    # calculate the corresponding 'coarseness' parameter used to generate
    # those files, and calculate the appropriate bin latency values:
    with open(ctx.FILE[0], 'r') as fp:
        global bin_vals,lower_bin_vals,upper_bin_vals,__HIST_COLUMNS,__TOTAL_COLUMNS
        __TOTAL_COLUMNS = len(fp.readline().split(','))
        __HIST_COLUMNS = __TOTAL_COLUMNS - __NON_HIST_COLUMNS

        max_cols = guess_max_from_bins(ctx, __HIST_COLUMNS)
        coarseness = int(np.log2(float(max_cols) / __HIST_COLUMNS))
        bin_vals = np.array(map(lambda x: plat_idx_to_val_coarse(x, coarseness), np.arange(__HIST_COLUMNS)), dtype=float)
        lower_bin_vals = np.array(map(lambda x: plat_idx_to_val_coarse(x, coarseness, 0.0), np.arange(__HIST_COLUMNS)), dtype=float)
        upper_bin_vals = np.array(map(lambda x: plat_idx_to_val_coarse(x, coarseness, 1.0), np.arange(__HIST_COLUMNS)), dtype=float)

    fps = [open(f, 'r') for f in ctx.FILE]
    gen = histogram_generator(ctx, fps, ctx.buff_size)

    print(', '.join(columns))

    try:
        start, end = 0, ctx.interval
        arr = np.empty(shape=(0,__TOTAL_COLUMNS - 1))
        more_data = True
        while more_data or len(arr) > 0:
            
            # Read up to ctx.max_latency (default 20 seconds) of data from end of current interval.
            while len(arr) == 0 or arr[-1][0] < ctx.max_latency * 1000 + end:
                try:
                    new_arr = next(gen)
                except StopIteration:
                    more_data = False
                    break
                arr = np.append(arr, new_arr.reshape((1,__TOTAL_COLUMNS - 1)), axis=0)
            arr = arr.astype(int)
            
            if arr.size > 0:
                # Jump immediately to the start of the input, rounding
                # down to the nearest multiple of the interval (useful when --log_unix_epoch
                # was used to create these histograms):
                if start == 0 and arr[0][0] - ctx.max_latency > end:
                    start = arr[0][0] - ctx.max_latency
                    start = start - (start % ctx.interval)
                    end = start + ctx.interval

                process_interval(ctx, arr, start, end)
                
                # Update arr to throw away samples we no longer need - samples which
                # end before the start of the next interval, i.e. the end of the
                # current interval:
                idx = np.where(arr[:,0] > end)
                arr = arr[idx]
            
            start += ctx.interval
            end = start + ctx.interval
    finally:
        map(lambda f: f.close(), fps)


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser()
    arg = p.add_argument
    arg("FILE", help='space separated list of latency log filenames', nargs='+')
    arg('--buff_size',
        default=10000,
        type=int,
        help='number of samples to buffer into numpy at a time')

    arg('--max_latency',
        default=20,
        type=float,
        help='number of seconds of data to process at a time')

    arg('-i', '--interval',
        type=int,
        help='interval width (ms), default 1000 ms')

    arg('-d', '--divisor',
        required=False,
        type=int,
        default=1,
        help='divide the results by this value.')

    arg('--decimals',
        default=3,
        type=int,
        help='number of decimal places to print floats to')

    arg('--warn',
        dest='warn',
        action='store_true',
        default=False,
        help='print warning messages to stderr')

    arg('--group_nr',
        default=29,
        type=int,
        help='FIO_IO_U_PLAT_GROUP_NR as defined in stat.h')

    arg('--job-file',
        default=None,
        type=str,
        help='Optional argument pointing to the job file used to create the '
             'given histogram files. Useful for auto-detecting --log_hist_msec and '
             '--log_unix_epoch (in fio) values.')

    main(p.parse_args())

