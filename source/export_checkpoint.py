"""Conservative, per-segment export checkpoints, including legacy adoption.

Only the original CPU stack graph defines picture identity. A different GPU
execution route may reuse that picture, but changed sources, effects, frame
clocks or encoder settings cannot. Completed files are retained independently
of the export's disposable working directory. No cache operation is required
for a render to succeed: all public operations fail closed to a cache miss.
"""
from __future__ import annotations

from contextlib import contextmanager
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
import uuid

SCHEMA = 1
_CREATE_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0
_ENCODER_FLAGS = {
    '-c:v', '-preset', '-tune', '-rc', '-b:v', '-cq', '-q:v', '-qp', '-crf',
    '-maxrate', '-bufsize', '-multipass', '-g', '-bf', '-profile:v',
    '-split_encode_mode', '-r', '-frames:v', '-video_track_timescale',
    '-fps_mode', '-spatial-aq', '-temporal-aq', '-aq-strength', '-rc-lookahead',
    '-weighted_pred', '-strict_gop', '-b_ref_mode', '-nonref_p', '-zerolatency',
    '-surfaces', '-level:v', '-tier', '-threads', '-x265-params', '-x264-params',
}
_IGNORED_OUTPUT_FLAGS = {
    '-gpu', '-pix_fmt', '-map_metadata',
}
_SIGNAL_FLAGS = {'-color_primaries': 'color_primaries', '-color_trc': 'color_trc',
                 '-colorspace': 'colorspace', '-color_range': 'range'}
_CODECS = {'hevc_nvenc': 'hevc', 'libx265': 'hevc', 'hevc_amf': 'hevc',
           'hevc_qsv': 'hevc', 'h264_nvenc': 'h264', 'libx264': 'h264',
           'h264_amf': 'h264', 'h264_qsv': 'h264', 'av1_nvenc': 'av1',
           'av1_amf': 'av1', 'av1_qsv': 'av1'}
_LUT = re.compile(r'lut3d=file=(.+?):interp=([A-Za-z_]+)')


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def _sha(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _path(value):
    return os.path.normcase(str(Path(value).resolve()))


def _stamp(path):
    info = Path(path).stat()
    return {'size': info.st_size, 'mtime_ns': info.st_mtime_ns}


def _unescape(value):
    # creative_core.escape_filter_path applies both FFmpeg parser layers.
    for _ in range(2):
        value = re.sub(r'\\(.)', r'\1', value)
    return value


class CheckpointStore:
    def __init__(self, root, output, ffprobe):
        self.output = _path(output)
        self.root = Path(root).resolve() / _sha(self.output)
        self.ffprobe = str(ffprobe)
        self._lock = threading.RLock()
        self._lut_cache = {}
        self._probe_cache = {}
        self._picture_index = None

    @staticmethod
    def _picture_key(render):
        picture = dict(render)
        picture['encoder'] = [pair for pair in render['encoder']
                              if pair[0] not in {'-b:v', '-maxrate', '-bufsize'}]
        return _sha(_json(picture))

    def transcode_candidate(self, command, expected_frames, width, height, pixel_format, rate):
        """Reuse composed pixels when only bitrate changes, by reencoding them.

        This never treats an old bitrate as a direct cache hit. The caller must
        run the requested encoder and commit a new checkpoint after success.
        """
        try:
            signature = self.signature(command)
            if signature is None or not self._unchanged(signature):
                return None
            expected = self._expected(signature, expected_frames, width, height, pixel_format, rate)
            options = dict(signature['render']['encoder'])
            if expected is None or not options.get('-b:v') or options.get('-c:v') not in {
                    'hevc_nvenc', 'h264_nvenc', 'av1_nvenc'}:
                return None
            with self._lock:
                if self._picture_index is None:
                    index = {}
                    for path in self.root.glob('*.json'):
                        try:
                            record = json.loads(path.read_text('utf-8'))
                            if record.get('schema') == SCHEMA:
                                index.setdefault(self._picture_key(record['render']), []).append(record)
                        except (OSError, ValueError, KeyError, TypeError):
                            continue
                    self._picture_index = index
                candidates = list(self._picture_index.get(self._picture_key(signature['render']), []))
            for record in candidates:
                if record['render'] == signature['render']:
                    continue
                old_options = dict(record['render']['encoder'])
                if not old_options.get('-b:v'):
                    continue
                old_signature = {'key': record['key'], 'render': record['render'],
                                 'dependencies': record.get('dependencies', [])}
                chunk = self._record(old_signature, expected, validate_probe=True)
                if chunk is not None and self._unchanged(signature):
                    return {'path': str(chunk), 'probe': record['probe'],
                            'previous_bitrate': old_options['-b:v']}
            return None
        except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
            return None

    def signature(self, command, graph_override=None, legacy_started_ns=None):
        """Return a semantic signature dict, or None for an unsafe/unknown route.

        graph_override is the saved *original CPU* .filtergraph path when a
        legacy command actually used a GPU graph or the GPU -vf fast path.
        The legacy timestamp guard applies to original media. LUT files may
        have been generated during export, but must predate the saved graph.
        Generated media newer than the old render are safely skipped.
        """
        try:
            command = [str(value) for value in command]
            graph_path = None
            if graph_override is not None:
                graph_path = Path(graph_override)
                graph = graph_path.read_text('utf-8-sig')
            elif '-/filter_complex' in command:
                graph_path = Path(command[command.index('-/filter_complex')+1])
                graph = graph_path.read_text('utf-8-sig')
            elif '-filter_complex' in command:
                graph = command[command.index('-filter_complex')+1]
            else:
                return None
            # Do not accidentally treat a reduced GPU graph, subtitle file or
            # arbitrary external filter input as the authoritative picture.
            if (not graph.startswith('color=c=black:') or any(value in graph for value in
                    ('hwdownload', 'scale_cuda', 'libplacebo', 'subtitles=', 'movie=', 'textfile='))):
                return None
            output_format = re.search(
                r'format=(yuv420p10le|yuv420p)(?:,setparams=[^\[\];]+)?\[picture\]\s*$', graph)
            canvas = re.search(r'^color=c=black:s=(\d+)x(\d+):r=([^,]+),', graph)
            if not output_format or not canvas:
                return None
            signal_match = re.search(r',setparams=([^\[\];]+)\[picture\]\s*$', graph)
            graph_signal = (dict(part.split('=', 1) for part in signal_match[1].split(':'))
                            if signal_match else {})
            dependencies = []
            lut_deadline = (graph_path.stat().st_mtime_ns if graph_path is not None
                            else legacy_started_ns) if legacy_started_ns is not None else None

            def lut_identity(match):
                path = Path(_unescape(match.group(1))).resolve(strict=True)
                state = _stamp(path)
                if lut_deadline is not None and state['mtime_ns'] > lut_deadline:
                    raise ValueError('LUT was modified after the saved render graph')
                cache_key = (str(path), state['size'], state['mtime_ns'])
                with self._lock:
                    digest = self._lut_cache.get(cache_key)
                if digest is None:
                    with path.open('rb') as handle:
                        digest = hashlib.file_digest(handle, 'sha256').hexdigest()
                    if state != _stamp(path):
                        raise ValueError('LUT changed while signing')
                    with self._lock:
                        self._lut_cache[cache_key] = digest
                dependencies.append({'path': _path(path), **state, 'sha256': digest})
                return 'lut3d=file=@sha256-'+digest+':interp='+match.group(2)

            graph = _LUT.sub(lut_identity, graph).strip()
            if 'file=' in graph.replace('lut3d=file=@sha256-', '') or 'filename=' in graph:
                return None
            sources = []
            seek = '0'
            decoder = None
            for index, token in enumerate(command[:-1]):
                if token == '-ss':
                    seek = str(Fraction(command[index+1]))
                elif token == '-c:v':
                    decoder = command[index+1]
                elif token == '-i':
                    path = Path(command[index+1]).resolve(strict=True)
                    if not path.is_file():
                        return None
                    state = _stamp(path)
                    if legacy_started_ns is not None and state['mtime_ns'] > legacy_started_ns:
                        return None
                    source = {'path': _path(path), 'seek': seek, **state}
                    if decoder is not None:
                        source['decoder'] = decoder
                    sources.append(source)
                    seek = '0'
                    decoder = None
            encoder = []
            # Alpha WebM inputs can select libvpx via an earlier -c:v. The
            # final occurrence belongs to the output encoder.
            index = max(index for index, value in enumerate(command) if value == '-c:v')
            while index < len(command)-1:
                flag = command[index]
                if index+1 >= len(command)-1:
                    return None
                value = command[index+1]
                if flag in _ENCODER_FLAGS:
                    if flag == '-r':
                        value = str(Fraction(value))
                    encoder.append([flag, value])
                elif flag in _SIGNAL_FLAGS:
                    # GPU routes repeat graph-proven signal tags on the
                    # encoder command. Ignore only these redundant tags;
                    # an explicit different range/color setting changes the
                    # delivered picture interpretation and must invalidate.
                    normalized = {'tv': 'limited', 'pc': 'full'}.get(value, value) if flag == '-color_range' else value
                    if graph_signal.get(_SIGNAL_FLAGS[flag]) != normalized:
                        encoder.append([flag, value])
                elif flag not in _IGNORED_OUTPUT_FLAGS:
                    return None
                index += 2
            options = dict(encoder)
            if not {'-c:v', '-r', '-frames:v'}.issubset(options) or options['-c:v'] not in _CODECS:
                return None
            render = {'schema': SCHEMA, 'graph_sha256': _sha(graph), 'sources': sources,
                      'encoder': encoder, 'width': int(canvas[1]), 'height': int(canvas[2]),
                      'pixel_format': output_format[1], 'rate': str(Fraction(canvas[3]))}
            if render['rate'] != options['-r']:
                return None
            return {'key': _sha(_json(render)), 'render': render, 'dependencies': dependencies}
        except (OSError, ValueError, TypeError, IndexError, KeyError, OverflowError):
            return None

    def _probe(self, path):
        path = Path(path)
        before = _stamp(path)
        if before['size'] <= 80:
            return None
        key = (_path(path), before['size'], before['mtime_ns'])
        with self._lock:
            if key in self._probe_cache:
                return self._probe_cache[key]
        result = subprocess.run([self.ffprobe, '-v', 'error', '-select_streams', 'v:0',
            '-show_entries', 'stream=codec_name,width,height,pix_fmt,avg_frame_rate,r_frame_rate,nb_frames,duration,color_range,color_space,color_transfer,color_primaries',
            '-of', 'json', str(path)], stdin=subprocess.DEVNULL, capture_output=True,
            timeout=15, creationflags=_CREATE_NO_WINDOW)
        if result.returncode or before != _stamp(path):
            return None
        streams = json.loads(result.stdout).get('streams', [])
        if len(streams) != 1:
            return None
        with self._lock:
            self._probe_cache[key] = streams[0]
        return streams[0]

    @staticmethod
    def _expected(signature, expected_frames, width, height, pixel_format, rate):
        render = signature['render']
        options = dict(render['encoder'])
        rate = str(Fraction(str(rate)))
        if (render['width'], render['height'], render['pixel_format'], render['rate'],
            int(options['-frames:v'])) != (width, height, pixel_format, rate, expected_frames):
            return None
        return {'frames': expected_frames, 'width': width, 'height': height,
                'pixel_format': pixel_format, 'rate': rate, 'codec': _CODECS[options['-c:v']]}

    @staticmethod
    def _matches(probe, expected):
        if not probe:
            return False
        try:
            rate = Fraction(expected['rate'])
            return (int(probe['nb_frames']) == expected['frames']
                and int(probe['width']) == expected['width']
                and int(probe['height']) == expected['height']
                and probe['pix_fmt'] == expected['pixel_format']
                and probe['codec_name'] == expected['codec']
                and Fraction(probe['avg_frame_rate']) == rate
                and abs(float(probe['duration'])-expected['frames']/float(rate)) < .002)
        except (KeyError, TypeError, ValueError, ZeroDivisionError, OverflowError):
            return False

    @staticmethod
    def _unchanged(signature):
        try:
            for item in [*signature['render']['sources'], *signature['dependencies']]:
                if _stamp(item['path']) != {'size': item['size'], 'mtime_ns': item['mtime_ns']}:
                    return False
            return True
        except OSError:
            return False

    def _record(self, signature, expected, *, validate_probe=False):
        path = self.root/(signature['key']+'.json')
        if not path.is_file() or path.stat().st_size > 2_000_000:
            return None
        record = json.loads(path.read_text('utf-8'))
        if (record.get('schema') != SCHEMA or record.get('key') != signature['key']
                or record.get('render') != signature['render'] or record.get('expected') != expected):
            return None
        name = record.get('chunk', '')
        if (Path(name).name != name or not re.fullmatch(signature['key']+r'-[a-f0-9]{32}\.mp4', name)):
            return None
        chunk = self.root/name
        if _stamp(chunk) != record.get('file_stat') or not self._matches(record.get('probe'), expected):
            return None
        if validate_probe:
            # A CPU stack can legitimately retain full-range pc metadata.
            # Preserve the observed signal rather than inventing a tv target.
            # Reuse still verifies that the saved signal has not changed.
            probe = self._probe(chunk)
            if not self._matches(probe, expected) or any(probe.get(field) != record['probe'].get(field)
                    for field in ('color_range', 'color_space', 'color_transfer', 'color_primaries')):
                return None
        return chunk

    @staticmethod
    def _link_or_copy(source, destination):
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(source, destination)
            return
        except FileExistsError:
            raise
        except OSError:
            pass
        created = False
        try:
            with destination.open('xb') as target:
                created = True
                with Path(source).open('rb') as handle:
                    shutil.copyfileobj(handle, target, 1024*1024)
            shutil.copystat(source, destination)
        except BaseException:
            if created:
                destination.unlink(missing_ok=True)
            raise

    @contextmanager
    def _entry_lock(self, key):
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root/(key+'.lock')
        handle = None
        deadline = time.monotonic()+2.
        while handle is None:
            try:
                handle = path.open('xb')
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise TimeoutError('checkpoint entry is busy')
                time.sleep(.05)
        try:
            yield
        finally:
            handle.close()
            path.unlink(missing_ok=True)

    def reuse(self, command, destination, expected_frames, width, height, pixel_format, rate):
        try:
            signature = self.signature(command)
            if signature is None:
                return False
            expected = self._expected(signature, expected_frames, width, height, pixel_format, rate)
            if expected is None:
                return False
            chunk = self._record(signature, expected, validate_probe=True)
            if chunk is None or not self._unchanged(signature):
                return False
            self._link_or_copy(chunk, destination)
            return True
        except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
            return False

    def commit(self, command, chunk, expected_frames, width, height, pixel_format, rate,
               *, graph_override=None, legacy_started_ns=None):
        try:
            signature = self.signature(command, graph_override, legacy_started_ns)
            if signature is None:
                return False
            expected = self._expected(signature, expected_frames, width, height, pixel_format, rate)
            if expected is None:
                return False
            with self._entry_lock(signature['key']):
                if self._record(signature, expected) is not None:
                    return True
                # Keep corrupt/unknown old records for inspection rather than
                # replacing them with a new interpretation of their key.
                manifest = self.root/(signature['key']+'.json')
                if manifest.exists():
                    return False
                probe = self._probe(chunk)
                if not self._matches(probe, expected) or not self._unchanged(signature):
                    return False
                before = _stamp(chunk)
                cached = self.root/(signature['key']+'-'+uuid.uuid4().hex+'.mp4')
                self._link_or_copy(chunk, cached)
                if before != _stamp(chunk) or before != _stamp(cached):
                    return False
                record = {'schema': SCHEMA, 'key': signature['key'], 'output': self.output,
                          'render': signature['render'], 'dependencies': signature['dependencies'],
                          'expected': expected, 'chunk': cached.name, 'file_stat': _stamp(cached),
                          'probe': probe, 'created_ns': time.time_ns()}
                pending = self.root/(signature['key']+'.'+uuid.uuid4().hex+'.pending')
                pending.write_text(_json(record)+'\n', encoding='utf-8')
                os.replace(pending, manifest)
                return True
        except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
            return False

    def capture_legacy(self, trace, directory):
        """Adopt only finalized, successful legacy chunks; never touch originals."""
        report = {'captured': 0, 'skipped': 0, 'pending': 0, 'store': str(self.root)}
        try:
            trace = Path(trace)
            directory = Path(directory).resolve(strict=True)
            stat = trace.stat()
            started_ns = getattr(stat, 'st_birthtime_ns', int(stat.st_ctime*1e9))
            starts = {}
            finishes = {}
            export = None
            for line in trace.read_text('utf-8').splitlines():
                try:
                    event = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if event.get('event') == 'export_start':
                    export = event
                elif event.get('event') == 'attempt_start':
                    task = event.get('task')
                    starts[task] = event
                    finishes.pop(task, None)
                elif event.get('event') == 'attempt_finish':
                    task = event.get('task')
                    if task in starts:
                        finishes[task] = event
                elif event.get('event') in ('task_error',):
                    finishes.pop(event.get('task'), None)
            if export is None or _path(export['output']) != self.output:
                return report
            for task, start in sorted(starts.items()):
                finish = finishes.get(task)
                if finish is None:
                    report['pending'] += 1
                    continue
                if finish.get('returncode') != 0 or not isinstance(task, int) or task < 1:
                    report['skipped'] += 1
                    continue
                command = start.get('command', [])
                chunk = directory/f'camera_{task-1:06d}.mp4'
                graph = directory/f'stack_{task-1:06d}.filtergraph'
                if not command or Path(command[-1]).resolve() != chunk or not graph.is_file():
                    report['skipped'] += 1
                    continue
                signature = self.signature(command, graph, started_ns)
                if signature is None:
                    report['skipped'] += 1
                    continue
                render = signature['render']
                accepted = self.commit(command, chunk, int(start['frames']),
                    int(export['width']), int(export['height']), render['pixel_format'],
                    export['rate'], graph_override=graph, legacy_started_ns=started_ns)
                report['captured' if accepted else 'skipped'] += 1
            return report
        except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
            return report


def capture_legacy(trace, directory, ffprobe, root=None):
    """Convenience entry point for a read-only running-export checkpoint saver."""
    try:
        for line in Path(trace).read_text('utf-8').splitlines():
            event = json.loads(line)
            if event.get('event') == 'export_start':
                if root is None:
                    from cache_paths import cache_dir
                    root = cache_dir('export-checkpoints')
                return CheckpointStore(root, event['output'], ffprobe).capture_legacy(trace, directory)
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return {'captured': 0, 'skipped': 0, 'pending': 0}
