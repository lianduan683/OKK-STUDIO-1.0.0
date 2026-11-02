"""Seek close to a long frame-phase prefix while retaining its original clock.

Do not classify millisecond timestamps as CFR. Locate the actual first decoded
frame of the reference seek, keep absolute timestamps, and run fps on exactly
that clock. A packet before the desired output interval supplies safe preroll.
"""
from fractions import Fraction
from functools import lru_cache
from pathlib import Path
import json
import re
import subprocess

FLAGS=0x08000000

@lru_cache(maxsize=128)
def _anchor(ffmpeg, path, size, stamp, seek, gpu):
    command=[ffmpeg,'-hide_banner','-loglevel','info','-copyts','-threads','2',
        '-hwaccel','cuda','-hwaccel_device',str(gpu),'-hwaccel_output_format','cuda',
        '-ss',seek,'-i',path,'-map','0:v:0','-an','-sn','-dn',
        '-vf','showinfo=checksum=0','-frames:v','1','-c:v','wrapped_avframe','-f','null','NUL']
    result=subprocess.run(command,stdin=subprocess.DEVNULL,capture_output=True,timeout=20,creationflags=FLAGS)
    if result.returncode:return None
    text=result.stderr.decode('utf8','replace')
    clock=re.search(r'config in time_base:\s*(\d+/\d+)',text)
    frame=re.search(r'\bn:\s*0\s+pts:\s*(-?\d+)\s+pts_time:',text)
    if not clock or not frame:return None
    return int(frame[1]),Fraction(clock[1])

def shorten_prefix(command, ffprobe, frames, rate_text, prefix, gpu_index=0):
    """Return an equivalent single-input command; uncertainty keeps the original."""
    if prefix < 120 or not ffprobe or not Path(ffprobe).is_file():return command
    try:
        rate=Fraction(rate_text)
        input_index=command.index('-i');seek_index=command.index('-ss')
        path=str(command[input_index+1]);seek=str(command[seek_index+1])
        stat=Path(path).stat()
        anchor=_anchor(str(command[0]),path,stat.st_size,stat.st_mtime_ns,seek,gpu_index)
        if anchor is None:return command
        anchor_pts,time_base=anchor
        header=subprocess.run([str(ffprobe),'-v','error','-show_entries','format=start_time',
            '-of','json',path],stdin=subprocess.DEVNULL,capture_output=True,timeout=15,creationflags=FLAGS)
        if header.returncode:return command
        start=Fraction(json.loads(header.stdout)['format'].get('start_time','0'))
        target=anchor_pts*time_base+prefix/rate
        proposed=max(Fraction(seek),target-start-2)
        packets=subprocess.run([str(ffprobe),'-v','error','-select_streams','v:0',
            '-read_intervals',f'{float(proposed):.9f}%+5','-show_packets','-show_entries','packet=pts_time',
            '-of','csv=p=0',path],stdin=subprocess.DEVNULL,capture_output=True,timeout=15,creationflags=FLAGS)
        if packets.returncode:return command
        previous=[]
        for line in packets.stdout.decode('utf8').splitlines():
            value=line.strip().split(',')[0]
            try:stamp=Fraction(value)
            except ValueError:continue
            if stamp <= target-1/rate:previous.append(stamp)
        if not previous:return command
        # Never seek past a decoded frame that might be needed for fps to
        # duplicate across a timestamp gap. Accurate seek can discard preroll.
        near=max(Fraction(seek),min(proposed,max(previous)-start-Fraction(1,10)))
        if near-Fraction(seek)<1:return command
        chain_index=command.index('-vf')+1
        chain=command[chain_index]
        old=f'setpts=PTS-STARTPTS,fps=fps={rate_text}:start_time=0,'
        if not chain.startswith(old):return command
        trim=f'trim=start_frame={prefix}:end_frame={prefix+frames}'
        if trim not in chain:return command
        chain=(f'setpts=PTS-{anchor_pts},fps=fps={rate_text}:start_time={float(prefix/rate):.12f},'
               +chain[len(old):])
        chain=chain.replace(trim,f'trim=end_frame={frames}',1)
        chain=re.sub(r'tpad=stop_mode=clone:stop_duration=[0-9.]+',
            f'tpad=stop_mode=clone:stop_duration={float(frames/rate+1):.9f}',chain,count=1)
        updated=list(command)
        updated[seek_index+1]=f'{float(near):.9f}'
        updated[chain_index]=chain
        updated[1:1]=['-copyts']
        if Path(path).stat().st_mtime_ns!=stat.st_mtime_ns:return command
        return updated
    except (OSError,ValueError,KeyError,IndexError,TypeError,subprocess.SubprocessError):
        return command
