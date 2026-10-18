"""Tiny filesystem fixtures; no Jianying process, media, or private report needed."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid

SOURCE = Path(__file__).resolve().parents[1] / 'scripts' / 'observe_cache.py'
spec = importlib.util.spec_from_file_location('observe_cache', SOURCE)
observer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(observer)
# A generated synthetic naming identity, never a project/task identifier.
SYNTHETIC_ID = str(uuid.UUID(int=1)).upper()

@unittest.skipUnless(os.name == 'posix', 'Observer uses POSIX no-follow directory descriptors')
class CacheObserverTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='cache-observer-fixture-')
        self.root = Path(self.temporary.name).resolve()
        self.bench = self.root / 'measurements'
        self.bench.mkdir()
        self.project = self.root / 'project'
        self.combo = self.project / 'Resources' / 'combination'
        self.combo.mkdir(parents=True)
        self.color_temp = self.combo / (SYNTHETIC_ID + '_video.mp4_temp.mp4')
        self.alpha_temp = self.combo / (SYNTHETIC_ID + '_video.mp4_temp.mp4.alpha.mp4')
        self.color = self.combo / (SYNTHETIC_ID + '_video.mp4')
        self.alpha = self.combo / (SYNTHETIC_ID + '_video.mp4.alpha.mp4')
        self.key = 'Resources/combination/' + SYNTHETIC_ID
        self.history = observer.ObservationHistory()
        self.previous, self.stable = {}, {}

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def write(path, size):
        path.write_bytes(b'x' * size)

    def sample(self):
        snapshot = observer.scan_caches(self.project)
        groups = observer.group_files(snapshot['files'], self.previous, self.stable)
        observation = self.history.update(snapshot['files'], groups)
        self.previous = groups
        return snapshot, groups, observation, observer.cache_counts(snapshot, groups, observation)

    def final_pair(self):
        self.write(self.color, 64)
        self.write(self.alpha, 16)
        return self.sample()

    def test_color_and_alpha_temporary_group_once(self):
        self.write(self.color_temp, 64)
        self.write(self.alpha_temp, 16)
        _, groups, observed, counts = self.sample()
        self.assertEqual(len(groups), 1)
        self.assertEqual({p['kind'] for p in groups[self.key]['parts']}, {'video', 'alpha'})
        self.assertEqual(counts['current_temporary_output_families'], 1)
        self.assertEqual(counts['current_named_final_color_alpha_pairs'], 0)
        self.assertEqual(counts['unique_observed_output_families'], 1)
        self.assertEqual(observed['lifetime_observed_positive_file_growth_bytes'], 0)

    def test_same_inode_temp_to_final_rename_does_not_add_growth(self):
        self.write(self.color_temp, 64)
        self.write(self.alpha_temp, 16)
        self.sample()
        self.write(self.color_temp, 96)
        self.write(self.alpha_temp, 24)
        self.assertEqual(self.sample()[2]['positive_file_growth_bytes_this_sample'], 40)
        identities = {(p.stat().st_dev, p.stat().st_ino) for p in (self.color_temp, self.alpha_temp)}
        self.color_temp.rename(self.color)
        self.alpha_temp.rename(self.alpha)
        _, groups, observed, counts = self.sample()
        self.assertEqual({(p.stat().st_dev, p.stat().st_ino) for p in (self.color, self.alpha)}, identities)
        self.assertEqual(counts['current_named_final_color_alpha_pairs'], 1)
        self.assertEqual(counts['unique_observed_output_families'], 1)
        self.assertEqual(observed['positive_file_growth_bytes_this_sample'], 0)
        self.assertEqual(observed['lifetime_observed_positive_file_growth_bytes'], 40)
        self.assertEqual(groups[self.key]['observable_attempt_sequence'], 1)

    def test_observed_truncation_and_regrowth(self):
        self.final_pair()
        self.write(self.color, 32)
        _, groups, observed, _ = self.sample()
        self.assertEqual(observed['positive_file_growth_bytes_this_sample'], 0)
        self.assertEqual(groups[self.key]['observable_attempt_sequence'], 2)
        self.assertEqual(observed['event_counts_lifetime']['physical_identity_size_decreased'], 1)
        self.write(self.color, 48)
        self.assertEqual(self.sample()[2]['positive_file_growth_bytes_this_sample'], 16)

    def test_observed_removal_and_recreation(self):
        self.final_pair()
        # Hold the old inode open so the fixture does not depend on inode reuse.
        with self.alpha.open('rb') as held:
            old_inode = os.fstat(held.fileno()).st_ino
            self.alpha.unlink()
            _, _, observed, counts = self.sample()
            self.assertEqual(counts['current_named_final_color_alpha_pairs'], 0)
            self.assertEqual(observed['event_counts_lifetime']['physical_identity_absent_this_sample'], 1)
            self.write(self.alpha, 12)
            self.assertNotEqual(self.alpha.stat().st_ino, old_inode)
            _, groups, observed, counts = self.sample()
            self.assertEqual(observed['positive_file_growth_bytes_this_sample'], 12)
            self.assertEqual(counts['unique_observed_output_families'], 1)
            self.assertIn('path_reappeared_after_observed_absence', groups[self.key]['observable_attempt_or_restart_reasons_this_sample'])

    def test_same_path_identity_replacement(self):
        self.final_pair()
        replacement = self.combo / 'replacement'
        self.write(replacement, 20)
        replacement.replace(self.color)
        _, groups, observed, _ = self.sample()
        self.assertEqual(observed['positive_file_growth_bytes_this_sample'], 20)
        self.assertEqual(groups[self.key]['observable_attempt_sequence'], 2)
        self.assertIn('path_identity_replaced', groups[self.key]['observable_attempt_or_restart_reasons_this_sample'])
        self.assertFalse(observed['limitations']['attempt_or_restart_is_native_job_identity'])

    def test_hard_links_do_not_double_count_growth(self):
        self.final_pair()
        linked = self.combo / 'physical-hard-link'
        os.link(self.color, linked)
        _, _, observed, counts = self.sample()
        self.assertEqual(counts['files'], 3)
        self.assertEqual(counts['current_physical_identities'], 2)
        self.assertEqual(observed['positive_file_growth_bytes_this_sample'], 0)
        self.write(linked, 80)
        self.assertEqual(self.sample()[2]['positive_file_growth_bytes_this_sample'], 16)

    def test_strict_suffixes_and_coexisting_names(self):
        self.final_pair()
        for name in ('not-an-id_video.mp4', SYNTHETIC_ID + '_video.mp4.bak',
                     SYNTHETIC_ID + '_video.mp4_temp.mp4.alpha.mp4.bak'):
            self.write(self.combo / name, 1)
        self.write(self.color_temp, 5)
        self.write(self.alpha_temp, 3)
        _, groups, _, counts = self.sample()
        self.assertEqual(counts['current_output_families'], 1)
        self.assertEqual(counts['unique_observed_output_families'], 1)
        self.assertEqual(counts['current_named_final_color_alpha_pairs'], 1)
        self.assertEqual(counts['current_temporary_output_families'], 1)
        self.assertEqual(len(groups[self.key]['parts']), 4)

    def test_internal_link_skipped_and_external_link_refused(self):
        self.final_pair()
        internal = self.combo / 'internal-link'
        internal.symlink_to(self.color.name)
        snapshot, _, _, _ = self.sample()
        self.assertEqual(len(snapshot['symlinks']), 1)
        self.assertFalse(snapshot['symlinks'][0]['followed'])
        self.assertNotIn('Resources/combination/internal-link', snapshot['files'])
        external_file = self.root / 'outside-project'
        self.write(external_file, 7)
        (self.combo / 'external-link').symlink_to(external_file)
        with self.assertRaises(observer.OwnershipError) as raised:
            observer.scan_caches(self.project)
        self.assertEqual(raised.exception.detail['reason'], 'cache_symlink_escapes_project')

    def test_output_ownership_and_cli_exclusive_read_only_run(self):
        physical = self.bench / 'physical'; physical.mkdir()
        linked = self.bench / 'linked'; linked.symlink_to(physical, target_is_directory=True)
        for path in (self.root / 'outside.jsonl', linked / 'log.jsonl'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                observer.check_owned_file(path, self.bench, 'fixture-output')
        app = self.root / 'Fixture.app'
        executable = app / 'Contents/MacOS/VideoFusion-macOS'
        executable.parent.mkdir(parents=True)
        executable.write_text('placeholder; never executed')
        self.final_pair()
        before = {p.name: (p.stat().st_size, p.stat().st_mtime_ns, p.read_bytes()) for p in (self.color, self.alpha)}
        output = self.bench / 'run.jsonl'
        command = [sys.executable, str(SOURCE), '--project', str(self.project), '--app', str(app),
                   '--bench-root', str(self.bench), '--output', str(output), '--duration', '0.3', '--interval', '0.25']
        result = subprocess.run(command, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(Path(str(output) + '.summary.json').read_text())
        self.assertFalse(summary['native_completion_or_codec_validated'])
        self.assertFalse(summary['application_mutated_or_killed'])
        self.assertEqual(summary['final_observation_telemetry']['lifetime_observed_positive_file_growth_bytes'], 0)
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        for path in (self.color, self.alpha):
            self.assertEqual((path.stat().st_size, path.stat().st_mtime_ns, path.read_bytes()), before[path.name])
        original_log = output.read_bytes()
        rerun = subprocess.run(command, capture_output=True, text=True, timeout=5)
        self.assertEqual(rerun.returncode, 2)
        self.assertEqual(output.read_bytes(), original_log)

if __name__ == '__main__':
    unittest.main()
