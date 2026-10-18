#!/usr/bin/env python3
"""Read-only physical cache observer for an explicitly selected Jianying project."""
import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time

GIB = 1024 ** 3
CACHE_ROOTS = {
    'combination': ('Resources/combination',),
    'audioAlg': ('Resources/audioAlg', 'audioAlg'),
    'videoAlg': ('Resources/videoAlg', 'videoAlg'),
    'loudness': ('Resources/loudness', 'loudness'),
}
UUID = r'[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}'
# Exact suffix allowlist, not an arbitrary trailing-suffix heuristic. MP4 names
# (including the alpha sidecar after the temp suffix) match observed Jianying
# output. Legacy separate alpha and audio names retain the documented formats.
PART_SUFFIXES = {
    '_video.mp4': ('video', False),
    '_video.mp4.alpha.mp4': ('alpha', False),
    '_video.mp4_temp.mp4': ('video', True),
    '_video.mp4_temp.mp4.alpha.mp4': ('alpha', True),
    '_alpha.mp4': ('alpha', False),
    '_alpha.mp4_temp.mp4': ('alpha', True),
    '_audio.m4a': ('audio', False),
    '_audio.m4a_temp.m4a': ('audio', True),
    '_audio.wav': ('audio', False),
    '_audio.wav_temp.wav': ('audio', True),
}
PART = re.compile(r'^(?P<uuid>' + UUID + r')(?P<suffix>' +
                  '|'.join(re.escape(s) for s in PART_SUFFIXES) + r')$')

OBSERVATION_LIMITS = {
    'scope': 'regular_physical_files_under_requested_project_cache_roots_only',
    'unique_output_family_identity': 'cache_relative_parent_plus_canonical_uuid',
    'named_final_pair_is_native_completion_proof': False,
    'growth_is_exact_io_or_all_task_count': False,
    'attempt_or_restart_is_native_job_identity': False,
    'growth_definition': ('Zero at first sample; thereafter sum positive size changes per '
                          '(device,inode), including the size of first-seen identities. '
                          'Same-inode renames and hard links are counted once. Observed '
                          'decreases add zero; later growth adds its positive delta. '
                          'A reappearing identity is compared with its last observed size.'),
    'blind_spots': ['writes_truncations_or_deletions_entirely_between_samples',
                   'same_size_rewrites', 'inode_reuse_between_observations',
                   'bytes_written_before_monitor_started', 'unwatched_paths_or_native_jobs',
                   'codec_or_native_completion_status'],
}

class OwnershipError(RuntimeError):
    def __init__(self, detail):
        self.detail = detail
        super().__init__(detail.get('reason', 'ownership not established'))

def inside(path, root):
    return path == root or root in path.parents

def wall_now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')

def check_owned_file(path, bench, label):
    raw = Path(path).expanduser()
    if raw.is_symlink():
        raise ValueError(label + ' must not be a symlink')
    result = raw.resolve()
    if not inside(result, bench) or result == bench or not result.parent.is_dir():
        raise ValueError(label + ' must have an existing physical parent inside --bench-root')
    # Disallow symlinked path components even when they resolve inside the bench.
    absolute = Path(os.path.abspath(raw))
    for parent in (absolute.parent, *absolute.parent.parents):
        if parent == bench:
            break
        if parent.is_symlink():
            raise ValueError(label + ' parent must not be a symlink')
    return result

def scan_caches(project):
    """Use no-follow directory descriptors; only stat regular physical files."""
    files, links, issues, roots = {}, [], [], []
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

    def issue(rel, operation, error):
        issues.append({'path': rel, 'operation': operation, 'errno': getattr(error, 'errno', None),
                       'error': type(error).__name__})

    def link(fd, name, rel):
        try:
            raw = os.readlink(name, dir_fd=fd)
            target = ((project / rel).parent / raw).resolve()
        except (OSError, RuntimeError) as exc:
            raise OwnershipError({'path': rel, 'reason': 'cannot_resolve_cache_symlink',
                                  'error': type(exc).__name__})
        item = {'path': rel, 'target': str(target), 'followed': False}
        links.append(item)
        if not inside(target, project):
            raise OwnershipError(dict(item, reason='cache_symlink_escapes_project'))

    def walk(fd, rel, family):
        try:
            entries = list(os.scandir(fd))
        except OSError as exc:
            issue(rel, 'scandir', exc)
            return
        for entry in entries:
            child_rel = rel + '/' + entry.name
            try:
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    link(fd, entry.name, child_rel)
                elif stat.S_ISDIR(info.st_mode):
                    child = os.open(entry.name, flags, dir_fd=fd)
                    try:
                        walk(child, child_rel, family)
                    finally:
                        os.close(child)
                elif stat.S_ISREG(info.st_mode):
                    files[child_rel] = {'family': family, 'size': info.st_size,
                                       'mtime_ns': info.st_mtime_ns, 'inode': info.st_ino,
                                       'device': info.st_dev, 'allocated_bytes': getattr(info, 'st_blocks', 0) * 512}
                else:
                    issues.append({'path': child_rel, 'operation': 'stat', 'error': 'special_file_skipped'})
            except OSError as exc:
                issue(child_rel, 'stat_or_open', exc)
                # A directory may have become a symlink after its first stat.
                try:
                    if stat.S_ISLNK(os.stat(entry.name, dir_fd=fd, follow_symlinks=False).st_mode):
                        link(fd, entry.name, child_rel)
                except OSError:
                    pass

    project_fd = os.open(project, flags)
    try:
        for family, candidates in CACHE_ROOTS.items():
            for rel in candidates:
                fd = os.dup(project_fd)
                current = []
                state = 'physical'
                try:
                    for component in Path(rel).parts:
                        current.append(component)
                        current_rel = '/'.join(current)
                        try:
                            info = os.stat(component, dir_fd=fd, follow_symlinks=False)
                            if stat.S_ISLNK(info.st_mode):
                                link(fd, component, current_rel)
                                state = 'internal_symlink_skipped'
                                break
                            child = os.open(component, flags, dir_fd=fd)
                        except FileNotFoundError:
                            state = 'missing'
                            break
                        except OSError as exc:
                            issue(current_rel, 'open_cache_root', exc)
                            state = 'unavailable'
                            try:
                                if stat.S_ISLNK(os.stat(component, dir_fd=fd, follow_symlinks=False).st_mode):
                                    link(fd, component, current_rel)
                            except OSError:
                                pass
                            break
                        os.close(fd)
                        fd = child
                    if state == 'physical':
                        walk(fd, rel, family)
                    roots.append({'family': family, 'path': rel, 'state': state})
                finally:
                    os.close(fd)
    finally:
        os.close(project_fd)
    return {'files': files, 'symlinks': links, 'issues': issues, 'roots': roots}

def group_files(files, previous, stable):
    groups = {}
    for rel, info in files.items():
        path = Path(rel)
        match = PART.match(path.name)
        if match:
            stem = match['uuid'].upper()
            kind, temporary = PART_SUFFIXES[match['suffix']]
            key = str(path.parent / stem)
            final_name = not temporary and info['size'] > 0
        else:
            key, kind, final_name, temporary = rel, 'other', False, False
        group = groups.setdefault(key, {'family': info['family'], 'bytes': 0, 'allocated_bytes': 0,
                                        'parts': [], 'final_color': False, 'final_alpha': False,
                                        'final_audio': False, 'canonical_output_family': bool(match),
                                        'canonical_uuid': match['uuid'].upper() if match else None,
                                        'has_temporary_parts': False})
        group['bytes'] += info['size']
        group['allocated_bytes'] += info['allocated_bytes']
        group['parts'].append({'path': rel, 'kind': kind, 'size': info['size'], 'mtime_ns': info['mtime_ns'],
                               'temporary': temporary, 'device': info['device'], 'inode': info['inode']})
        group['has_temporary_parts'] |= temporary
        if final_name and kind in ('video', 'alpha', 'audio'):
            group[{'video': 'final_color', 'alpha': 'final_alpha', 'audio': 'final_audio'}[kind]] = True
    for key, group in groups.items():
        group['parts'].sort(key=lambda part: part['path'])
        old = previous.get(key)
        unchanged = old is not None and old['parts'] == group['parts']
        stable[key] = stable.get(key, 1) + 1 if unchanged else 1
        group['stable_observations'] = stable[key]
        group['delta_bytes'] = group['bytes'] - (old['bytes'] if old else 0)
        group['named_color_alpha_pair'] = group['final_color'] and group['final_alpha']
        group['stat_stable_color_alpha_pair'] = group['named_color_alpha_pair'] and stable[key] >= 2
        group['stat_stable_audio'] = group['final_audio'] and stable[key] >= 2
    for key in list(stable):
        if key not in groups:
            del stable[key]
    return groups


class ObservationHistory:
    """Sampled stat evidence; never infer native jobs or completion from files."""
    def __init__(self):
        self.sample_index = 0
        self.identities = {}
        self.previous_ids = set()
        self.path_history = {}
        self.previous_paths = set()
        self.observed_families = set()
        self.previous_families = set()
        self.family_sequence = {}
        self.positive_growth_bytes = 0
        self.total_events = {}
        self.baseline_identities = 0

    def update(self, files, groups):
        self.sample_index += 1
        first = self.sample_index == 1
        events, family_reasons = [], {}
        by_identity = {}
        path_family = {}
        for key, group in groups.items():
            if group['canonical_output_family']:
                for part in group['parts']:
                    path_family[part['path']] = key

        def record(reason, **fields):
            event = {'reason': reason, **fields}
            events.append(event)
            self.total_events[reason] = self.total_events.get(reason, 0) + 1
            for path in event.get('paths', []):
                key = path_family.get(path)
                if key is not None:
                    family_reasons.setdefault(key, set()).add(reason)

        for rel, info in files.items():
            identity = (info['device'], info['inode'])
            item = by_identity.setdefault(identity, {'size': info['size'], 'paths': []})
            item['size'] = max(item['size'], info['size'])
            item['paths'].append(rel)
            old_identity = self.path_history.get(rel)
            if old_identity is not None:
                if rel not in self.previous_paths:
                    record('path_reappeared_after_observed_absence', paths=[rel],
                           previous_identity=list(old_identity), identity=list(identity))
                elif old_identity != identity:
                    record('path_identity_replaced', paths=[rel],
                           previous_identity=list(old_identity), identity=list(identity))
            self.path_history[rel] = identity

        growth_this_sample = 0
        for identity, item in by_identity.items():
            item['paths'].sort()
            old = self.identities.get(identity)
            if old is None:
                if not first:
                    growth_this_sample += item['size']
            else:
                change = item['size'] - old['size']
                growth_this_sample += max(0, change)
                if change < 0:
                    record('physical_identity_size_decreased', paths=item['paths'], identity=list(identity),
                           previous_size=old['size'], size=item['size'], decreased_bytes=-change)
                if identity not in self.previous_ids:
                    record('physical_identity_reappeared_after_observed_absence', paths=item['paths'],
                           identity=list(identity), previous_size=old['size'], size=item['size'])
                elif item['paths'] != old['paths']:
                    # This includes temp->final rename and hard-link path changes.
                    record('physical_identity_paths_changed', paths=item['paths'],
                           previous_paths=old['paths'], identity=list(identity),
                           counted_as_new_identity=False)
            self.identities[identity] = item

        removed_ids = self.previous_ids - set(by_identity)
        for identity in sorted(removed_ids):
            record('physical_identity_absent_this_sample', paths=self.identities[identity]['paths'],
                   identity=list(identity), previously_observed_size=self.identities[identity]['size'])
        current_families = {key for key, group in groups.items() if group['canonical_output_family']}
        for key in sorted(current_families):
            if key not in self.observed_families:
                self.family_sequence[key] = 1
            else:
                if key not in self.previous_families:
                    family_reasons.setdefault(key, set()).add('family_reappeared_after_observed_absence')
                # A rename/normal removal alone is not restart evidence.
                restart_reasons = family_reasons.get(key, set()) - {
                    'physical_identity_paths_changed', 'physical_identity_absent_this_sample'}
                if restart_reasons:
                    self.family_sequence[key] += 1
            groups[key]['observable_attempt_sequence'] = self.family_sequence[key]
            groups[key]['observable_attempt_or_restart_reasons_this_sample'] = sorted(
                family_reasons.get(key, set()) - {
                    'physical_identity_paths_changed', 'physical_identity_absent_this_sample'})
        self.observed_families.update(current_families)
        self.positive_growth_bytes += growth_this_sample
        self.previous_ids = set(by_identity)
        self.previous_paths = set(files)
        self.previous_families = current_families
        if first:
            self.baseline_identities = len(by_identity)
        return {
            'sample_index': self.sample_index,
            'unique_observed_output_families': len(self.observed_families),
            'current_output_families': len(current_families),
            'current_named_final_color_alpha_pairs': sum(
                groups[key]['named_color_alpha_pair'] for key in current_families),
            'current_temporary_output_families': sum(
                groups[key]['has_temporary_parts'] for key in current_families),
            'current_named_final_audio_families': sum(groups[key]['final_audio'] for key in current_families),
            'lifetime_observed_positive_file_growth_bytes': self.positive_growth_bytes,
            'positive_file_growth_bytes_this_sample': growth_this_sample,
            'baseline_observed_physical_identities': self.baseline_identities,
            'unique_observed_physical_identities': len(self.identities),
            'current_physical_identities': len(by_identity),
            'current_regular_paths': len(files),
            'observable_attempt_sequences': dict(sorted(self.family_sequence.items())),
            'event_counts_lifetime': dict(sorted(self.total_events.items())),
            'filesystem_observations_this_sample': events,
            'limitations': OBSERVATION_LIMITS,
        }

def cache_counts(snapshot, groups, observation=None):
    counts = {'files': len(snapshot['files']), 'logical_bytes': sum(x['size'] for x in snapshot['files'].values()),
            'allocated_bytes': sum(x['allocated_bytes'] for x in snapshot['files'].values()),
            'named_color_alpha_pairs': sum(g['named_color_alpha_pair'] for g in groups.values()),
            'stat_stable_color_alpha_pairs': sum(g['stat_stable_color_alpha_pair'] for g in groups.values()),
            'named_audio_groups': sum(g['final_audio'] for g in groups.values()),
            'stat_stable_audio_groups': sum(g['stat_stable_audio'] for g in groups.values()),
            'other_file_groups': sum(not any(p['kind'] != 'other' for p in g['parts']) for g in groups.values())}
    if observation is not None:
        for key in ('unique_observed_output_families', 'current_output_families',
                    'current_named_final_color_alpha_pairs', 'current_temporary_output_families',
                    'current_named_final_audio_families', 'lifetime_observed_positive_file_growth_bytes',
                    'positive_file_growth_bytes_this_sample', 'current_physical_identities'):
            counts[key] = observation[key]
    return counts

def main_processes(executable):
    try:
        result = subprocess.run(['/bin/ps', '-ww', '-axo', 'pid=,pcpu=,rss=,comm='], capture_output=True,
                                text=True, timeout=3, check=True)
        matched = []
        for row in result.stdout.splitlines():
            fields = row.strip().split(None, 3)
            if len(fields) != 4 or os.path.normpath(fields[3]) != str(executable):
                continue
            matched.append({'pid': int(fields[0]), 'pcpu_ps': float(fields[1]),
                            'rss_bytes': int(fields[2]) * 1024})
        return {'state': 'matched' if matched else 'exact_main_executable_not_found', 'processes': matched}
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return {'state': 'process_read_error', 'error': type(exc).__name__, 'processes': []}

def disks(paths):
    rows = []
    for role, path in paths:
        try:
            data = os.statvfs(path)
            rows.append({'role': role, 'path': str(path), 'device': os.stat(path).st_dev,
                         'free_bytes_available': data.f_bavail * data.f_frsize,
                         'total_bytes': data.f_blocks * data.f_frsize})
        except OSError as exc:
            rows.append({'role': role, 'path': str(path), 'error': type(exc).__name__, 'errno': exc.errno})
    return rows

def atomic_summary(path, summary):
    # Publish a complete new file atomically without overwriting another run.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.monitor-summary-', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(summary, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

def run_monitor(args):
    raw_project = args.project.expanduser()
    if raw_project.is_symlink():
        raise ValueError('--project root must not be a symlink')
    project = raw_project.resolve(strict=True)
    bench = args.bench_root.expanduser().resolve(strict=True)
    if not project.is_dir() or not bench.is_dir() or inside(bench, project):
        raise ValueError('Project and writable bench directories must be distinct; bench cannot be inside project')
    output = check_owned_file(args.output, bench, '--output')
    summary_path = check_owned_file(args.summary or Path(str(output) + '.summary.json'), bench, '--summary')
    stop_file = check_owned_file(args.stop_file, bench, '--stop-file') if args.stop_file else None
    if any(path is not None and inside(path, project) for path in (output, summary_path, stop_file)):
        raise ValueError('Monitor output/control files must not be inside the project')
    if output == summary_path or stop_file in (output, summary_path):
        raise ValueError('Output, summary and stop file paths must be distinct')
    if output.exists() or summary_path.exists():
        raise ValueError('Refusing existing output or summary')
    app = args.app.expanduser().resolve(strict=True)
    executable = app / 'Contents/MacOS/VideoFusion-macOS' if app.suffix == '.app' else app
    executable = executable.resolve(strict=True)
    if executable.name != 'VideoFusion-macOS' or not executable.is_file():
        raise ValueError('--app must select the exact main VideoFusion-macOS executable or its .app')
    disk_paths = [('project_volume', project), ('bench_volume', bench)]
    disk_paths.extend(('additional_' + str(i + 1), p.expanduser().resolve(strict=True)) for i, p in enumerate(args.disk))
    started, started_wall = time.monotonic(), wall_now()
    previous, stable, last_counts = {}, {}, {}
    history, final_observation = ObservationHistory(), {}
    baseline_bytes = peak_bytes = sample_count = 0
    warned = set()
    reason = 'duration_elapsed'
    interrupted = {'signal': None}
    last_disks, final_groups = [], {}
    old_handlers = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        old_handlers[signum] = signal.signal(signum, lambda signum, frame: interrupted.update(signal=signum))
    output_fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(output_fd, 'w', encoding='utf-8') as stream:
        def emit(kind, **fields):
            row = {'event': kind, 'wall_utc': wall_now(), 'monotonic_seconds': time.monotonic(),
                   'elapsed_seconds': time.monotonic() - started}
            row.update(fields)
            stream.write(json.dumps(row, ensure_ascii=False) + '\n')
            stream.flush()

        emit('start', schema_version=2, project=str(project), app_executable=str(executable),
             duration_seconds=args.duration, interval_seconds=args.interval,
             planned_new_bytes=None if args.planned_new_gib is None else args.planned_new_gib * GIB,
             warn_free_bytes=None if args.warn_free_gib is None else args.warn_free_gib * GIB,
             request_stop_free_bytes=None if args.request_stop_free_gib is None else args.request_stop_free_gib * GIB,
             ownership='not_yet_scanned', mutates_application=False, auto_cancels_tasks=False,
             observation_limits=OBSERVATION_LIMITS)
        next_sample = started
        try:
            while time.monotonic() - started < args.duration:
                if interrupted['signal'] is not None:
                    reason = 'monitor_signal_' + str(interrupted['signal'])
                    break
                if stop_file is not None and os.path.lexists(stop_file):
                    emit('stop_file_observed', path=str(stop_file), action='stop_monitor_only')
                    reason = 'stop_file'
                    break
                now = time.monotonic()
                if now < next_sample:
                    time.sleep(min(5.0, next_sample - now, max(0.0, args.duration - (now - started))))
                    continue
                sample_started = time.monotonic()
                snapshot = scan_caches(project)
                groups = group_files(snapshot['files'], previous, stable)
                final_observation = history.update(snapshot['files'], groups)
                counts = cache_counts(snapshot, groups, final_observation)
                if sample_count == 0:
                    baseline_bytes = counts['logical_bytes']
                peak_bytes = max(peak_bytes, counts['logical_bytes'])
                sample_count += 1
                last_disks = disks(disk_paths)
                for disk in last_disks:
                    free = disk.get('free_bytes_available')
                    if free is None:
                        continue
                    for label, threshold in (('request_native_stop', args.request_stop_free_gib),
                                             ('warn_low_free', args.warn_free_gib)):
                        key = (disk.get('device'), label)
                        if threshold is not None and free < threshold * GIB and key not in warned:
                            emit('disk_threshold', level=label, disk=disk,
                                 threshold_bytes=threshold * GIB,
                                 action='notify_operator_only_no_app_or_monitor_kill')
                            warned.add(key)
                removed = sorted(set(previous) - set(groups))
                emit('sample', sample_index=sample_count, ownership='physical_cache_roots_only',
                     sample_started_elapsed_seconds=sample_started - started,
                     cache_counts=counts, logical_growth_since_start=counts['logical_bytes'] - baseline_bytes,
                     observation_telemetry=final_observation,
                     groups=groups, removed_groups=removed, cache_roots=snapshot['roots'],
                     symlinks=snapshot['symlinks'], stat_issues=snapshot['issues'],
                     disks=last_disks, main_app=main_processes(executable),
                     scan_seconds=time.monotonic() - sample_started)
                previous, final_groups, last_counts = groups, groups, counts
                next_sample += args.interval
                if next_sample <= time.monotonic():
                    next_sample = time.monotonic() + args.interval  # skip overdue work, never spin to catch up
        except OwnershipError as exc:
            reason = 'refused_cache_ownership'
            emit('ownership_refusal', detail=exc.detail, action='stop_monitor_only')
        except (OSError, RuntimeError) as exc:
            reason = 'monitor_error'
            emit('monitor_error', error=type(exc).__name__, message=str(exc))
        finally:
            summary = {'schema_version': 2, 'reason': reason, 'wall_started_utc': started_wall,
                       'wall_finished_utc': wall_now(), 'elapsed_seconds': time.monotonic() - started,
                       'sample_count': sample_count, 'baseline_logical_bytes': baseline_bytes,
                       'peak_logical_bytes': peak_bytes, 'final_cache_counts': last_counts,
                       'final_groups': final_groups, 'final_disks': last_disks,
                       'final_observation_telemetry': final_observation,
                       'native_completion_or_codec_validated': False,
                       'application_mutated_or_killed': False}
            emit('terminal', **summary)
            os.fsync(stream.fileno())
            try:
                atomic_summary(summary_path, summary)
            finally:
                for signum, handler in old_handlers.items():
                    signal.signal(signum, handler)
    return 2 if reason in ('refused_cache_ownership', 'monitor_error') else 0

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--app', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--bench-root', type=Path, required=True, help='Existing owned measurement directory outside the project')
    parser.add_argument('--summary', type=Path)
    parser.add_argument('--stop-file', type=Path)
    parser.add_argument('--duration', type=float, default=7200)
    parser.add_argument('--interval', type=float, default=5)
    parser.add_argument('--disk', type=Path, action='append', default=[])
    parser.add_argument('--warn-free-gib', type=float)
    parser.add_argument('--request-stop-free-gib', type=float)
    parser.add_argument('--planned-new-gib', type=float)
    args = parser.parse_args(argv)
    if not math.isfinite(args.duration) or not 0 < args.duration <= 43200:
        parser.error('--duration must be finite and in (0, 43200] seconds')
    if not math.isfinite(args.interval) or not 0.25 <= args.interval <= 5:
        parser.error('--interval must be finite and between 0.25 and 5 seconds')
    for name in ('warn_free_gib', 'request_stop_free_gib', 'planned_new_gib'):
        value = getattr(args, name)
        if value is not None and (not math.isfinite(value) or value < 0):
            parser.error(name + ' must be finite and non-negative')
    if args.warn_free_gib is not None and args.request_stop_free_gib is not None and args.request_stop_free_gib > args.warn_free_gib:
        parser.error('Stop-request threshold must not exceed warning threshold')
    try:
        return run_monitor(args)
    except (OSError, ValueError, RuntimeError) as exc:
        print('MONITOR REFUSED: ' + str(exc), file=sys.stderr)
        return 2

if __name__ == '__main__':
    sys.exit(main())
