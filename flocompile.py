#!/usr/bin/env python3
# Basic CSV -> machine code compiler for flocra

import numpy as np
import warnings
from flomachine import *
from local_config import grad_board

import pdb
st = pdb.set_trace

grad_data_bufs = (1, 2)

def debug_print(*args, **kwargs):
    # print(*args, **kwargs)
    pass

def col2buf(col_idx, value, gb=grad_board):
    """ Returns a tuple of (buffer indices), (values), (value masks) 
    A value masks specifies which bits are actually relevant on the output."""
    if col_idx in (1, 2, 3, 4): # TX
        buf_idx = col_idx + 4,
        val = value,
        mask = 0xffff,
    elif col_idx in (5, 6, 7, 8, 9, 10, 11, 12): # grad
        # Only encode value and channel into words here.  Precise
        # timing and broadcast logic will be handled at the next stage
        if gb == "gpa-fhdo":
            if col_idx in (9, 10, 11, 12):
                raise RuntimeError("GPA-FHDO is selected, but CSV is trying to control OCRA1")
            grad_chan = col_idx - 5
            val_full = value | 0x80000 | ( grad_chan << 16 ) | (grad_chan << 25)
        elif gb == "ocra1":
            if col_idx in (5, 6, 7, 8):
                raise RuntimeError("OCRA1 is selected, but CSV is trying to control GPA-FHDO")
            grad_chan = col_idx - 9                
            val_full = value << 2 | 0x00100000 | (grad_chan << 25)
        else:
            raise ValueError("Unknown grad board")

        buf_idx = 1, 2
        val = val_full & 0xffff, val_full >> 16
        mask = 0xffff, 0xffff
    elif col_idx in (13, 14): # RX rate
        buf_idx = col_idx - 10,
        val = value,
        mask = 0x0fff,
    elif col_idx in (15, 16): # RX rate valid
        buf_idx = col_idx - 12,
        val = value << 14,
        mask = 0x1 << 14,
    elif col_idx in (17, 18): # RX resets, active low
        buf_idx = col_idx - 14,
        val = value << 15,
        mask = 0x1 << 15,
    elif col_idx in (19, 20, 21): # TX/RX gates, external trig
        buf_idx = 15,
        bit_idx = col_idx - 19
        val = value << bit_idx,
        mask = 0x1 << bit_idx,
    elif col_idx == 22: # LEDs
        buf_idx = 15,
        val = value << 8,
        mask = 0xff00,

    return buf_idx, val, mask

def csv2bin(path, quick_start=False, min_grad_clocks=200,
            initial_bufs=np.zeros(16, dtype=np.uint16),            
            latencies = np.zeros(16, dtype=np.int32)):
    """ initial_bufs: starting state of output buffers, to track with instructions
    quick_start: strip out the initial RAM-writing dead time if the CSV was generated by the simulator or similar
    min_grad_clocks: for gradient operations, how often can a full 32-bit word be sent without saturating the serialiser"""

    # Input: CSV column, starting from 0 for tx0 i and ending with 21 for leds
    # Output: corresponding buffer index or indices to change
    
    data = np.loadtxt(path, skiprows=1, delimiter=',', comments='#').astype(np.uint32)
    with open(path, 'r') as csvf:
        cols = csvf.readline().strip().split(',')[1:]

    assert cols[-1] == ' csv_version_0.1', "Wrong CSV format"

    # latencies for each of the data buffers
    # Grad latencies are the longest
    # RX control latencies are just to match the RX reset timing
    # latencies = np.array([0, 150, 150, 20, 20, 0, 0, 0, 0, 2, 2, 2, 2, 2, 2, 0]) # original
    # latencies = np.array([0, 1, 1, 1, 1, 0, 0, 0, 0, 2, 2, 2, 2, 2, 2, 0])
    # latencies = np.array([0, 0, 0, 0, 0, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 0])
    
    # latencies = np.array([0, 0, 0, 0, 0, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0])    
    # latencies = np.array([0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 0])
    # latencies = np.array([0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1])

    if quick_start:
        # remove dead time in the beginning taken up by simulated memory writes, if the input CSV is generated from simulation
        # data[1:, 0] = data[1:, 0] - data[1, 0] + latencies.max() 
        data[1:, 0] = data[1:, 0] - data[1, 0] + 10

    # Boolean: compare data offset by one row in time
    data_diff = data[:-1,1:] != data[1:,1:]

    changelist = []
    changelist_grad = []
    
    for k, dd in enumerate(data_diff):
        clocktime = data[k + 1, 0]
        dw = np.where(dd)[0] # indices where data changed
        for col_idx, value in zip(dw + 1, data[k + 1][dw + 1]):
            buf_idces, vals, masks = col2buf(col_idx, value)
            for bi, v, m in zip(buf_idces, vals, masks):
                change = clocktime - latencies[bi], bi, v, m
                if bi in grad_data_bufs:
                    changelist_grad.append(change)
                else:
                    changelist.append(change)

    ## TODO: intelligently remove and replace gradient outputs that are
    ## simultaneous, and split them up to use the broadcasts
                
    # Write out initial values
    bdata = []
    addr = 0
    states = initial_bufs
    for k, ib in enumerate(initial_bufs):
        bdata.append(instb(k, 15-k, ib))

    # TODO: process the grad changelist, depending on what GPA is being used etc
    # changelist_grad.sort(key=sortfn) # sort by time
    # TODO: append the grad changelist to the main one
    
    # sort the change list
    sortfn = lambda change: change[0]
    changelist.sort(key=sortfn) # sort by time
    
    # Process and combine the change list into discrete sets of operations at each time, i.e. an output list
    def cl2ol(changelist):
        current_bufs = initial_bufs.copy()
        current_time = changelist[0][0]
        unique_times = []
        unique_changes = []
        change_masks = np.zeros(16, dtype=np.uint16)
        changed = np.zeros(16, dtype=bool)

        def close_timestep(time):
            ch_idces = np.where(changed)[0]
            # buf_time_offsets = np.zeros(16, dtype=int32)
            buf_time_offsets = 0
            unique_changes.append( [time, ch_idces, current_bufs[ch_idces], buf_time_offsets] )
            change_masks[:] = np.zeros(16, dtype=np.uint16)
            changed[:] = np.zeros(16, dtype=bool)            
        
        for time, buf, val, mask in changelist:
            if time != current_time:
                close_timestep(current_time)
                current_time = time
            buf_diff = (current_bufs[buf] ^ val) & mask
            assert buf_diff & change_masks[buf] == 0, "Tried to set a buffer to two values at once"
            if buf_diff == 0:
                warnings.warn("Instruction will have no effect. Skipping...")
                continue
            val_masked = val & mask
            old_val_unmasked = current_bufs[buf] & ~mask
            new_val = old_val_unmasked | val_masked
            change_masks[buf] |= mask            
            current_bufs[buf] = new_val
            changed[buf] = True
            
        close_timestep(current_time)
            
        return unique_changes

    changes = cl2ol(changelist)
    
    # Process time offsets
    for ch, ch_prev in zip( reversed(changes[1:]), reversed(changes[:-1]) ):
        # does the current timestep need to output more data than can
        # fit into the time gap since the previous timestep?
        timestep = ch[0] - ch_prev[0]
        timediff = ch[1].size - timestep
        # if timestep < ch[1].size: # not enough time 
            
        if timediff > 0:
            ch_prev[0] -= timediff # move prev. event into the past
            ch_prev[3] = timediff # make prev. event's buffers output in its future

    # convert to differential timesteps
    last_time = 0
    for ch in changes:
        ch0 = ch[0]
        ch[0] = ch0 - last_time
        last_time = ch0

    # Interpretation of each element of changes list:
    # [time when all instructions for this change will have completed,
    #  buffers that need to be changed,
    #  values to set the buffers to,
    #  the delay until the buffers will output their values]
    
    # Write out instructions

    last_buf_time_left = np.zeros(16, dtype=np.int32)
    buf_time_left = np.zeros(16, dtype=np.int32)
    # buf_empty_time = np.zeros(16, dtype=np.int32)
    debug_print("changes:")
    for k in changes:
        debug_print(k)
    
    for event in changes:
        b_instrs = event[1].size
        dtime = event[0]

        # soak up any extra time which is in excess of what the instructions need to execute synchronously
        excess_dtime = dtime - b_instrs
        excess_dtime_tmp = excess_dtime
        while excess_dtime_tmp > 2: # delay of 3 or more cycles needed
            wait_time = min(excess_dtime_tmp, COUNTER_MAX + 3) # delay for the time instruction
            bdata.append(insta(IWAIT, wait_time - 3))
            excess_dtime_tmp -= wait_time
            debug_print("i wait ", wait_time - 3)
        if excess_dtime_tmp: # final delay of 1 or 2 cycles
            for k in range(dtime - b_instrs):
                debug_print("i nop")
                bdata.append(insta(INOP, 0))

        # time left after delays from nops or waits
        # dtime_eff could be increased later with a more advanced
        # compiler, to make the buffers bear more of the internal
        # delays
        # dtime_eff = b_instrs

        # count down the times until each channel buffer will be empty
        buf_time_left -= excess_dtime
        buf_time_left[buf_time_left < 0] = 0
        this_time_offset = event[3]
        debug_print("--- dtime {:2d}, this_time_offset: {:2d}, b_instrs: {:2d}, lbtl: ".format(dtime, this_time_offset, b_instrs), last_buf_time_left[5:9])
        for m, (ind, dat) in enumerate(zip(event[1], event[2])):
            execution_delay = b_instrs - m - 1 #+ time - 2
            btli = buf_time_left[ind]
            buf_empty = btli <= m # or <= m, need to check
            if buf_empty: # buffer empty for this instruction; need an appropriate delay only for sync
                # (check against m since with successive cycles, remaining buffers will empty out)
                extra_delay = execution_delay + this_time_offset
                buf_time_left[ind] = this_time_offset + b_instrs
            else:
                # buffer already not empty on this cycle
                extra_delay = this_time_offset - btli + b_instrs - 1
                buf_time_left[ind] += extra_delay + 1

            debug_print("bti={:d} btli={:d} m={:d} empty={:d} edel={:d} instb i {:d} del {:d} dat {:d}".format(
                buf_time_left[ind], btli, m, buf_empty, execution_delay, ind, extra_delay, dat))
            bdata.append(instb(ind, extra_delay, dat))

        buf_time_left -= b_instrs # take into account execution time of this timestep

    if False: # Manual instructions for debugging
        # case 1
        # bdata.append(instb(5, 2, 1))
        # bdata.append(instb(6, 1, 2))
        # bdata.append(instb(5, 0, 3))
        # bdata.append(instb(5, 0, 4))
        # bdata.append(instb(6, 0, 5))

        # case 2
        bdata.append(instb(5, 2, 1))
        bdata.append(instb(6, 1, 2))
        bdata.append(instb(5, 1, 3))
        bdata.append(instb(5, 1, 4))
        bdata.append(instb(6, 0, 5))
    # bdata.append(instb(5, 2, 1))
    # bdata.append(instb(5, 2, 2))
    # bdata.append(instb(5, 1, 3))        
    return bdata
                
if __name__ == "__main__":
    csv2bin("/tmp/flocra.csv")
