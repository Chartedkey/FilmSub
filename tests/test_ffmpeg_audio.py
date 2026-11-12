from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from filmsub import sync


class FfmpegAudioTests(unittest.TestCase):
    def test_dvd_single_audio_bypasses_remux(self):
        with patch.object(sync, '_run_ffmpeg') as run:
            ref = sync._join_audio(['part_0.flac'], 'ffmpeg', 'unused')
        self.assertEqual(ref, 'part_0.flac')
        run.assert_not_called()

    def test_ffmpeg_error_includes_return_code_and_stderr(self):
        command = [sys.executable, '-c', 'import sys; sys.stderr.write("Disk full: cannot write output\\n"); sys.exit(3)']
        with self.assertRaisesRegex(sync.SyncError, 'код 3.*Disk full'):
            sync._run_ffmpeg(command, None, 'ffmpeg failed')

    def test_cancellation_kills_ffmpeg(self):
        proc = Mock()
        proc.poll.return_value = None
        with patch.object(sync.subprocess, 'Popen', return_value=proc):
            with self.assertRaisesRegex(sync.SyncError, 'Остановлено'):
                sync._run_ffmpeg(['ffmpeg'], lambda: True, 'failed')
        proc.kill.assert_called_once()
        proc.wait.assert_called_once()

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'requires ffmpeg')
    def test_join_native_flac_in_unicode_temp_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / 'Звук фильма'
            folder.mkdir()
            pieces = []
            for i in range(2):
                dest = folder / f'part_{i}.flac'
                subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i',
                                f'sine=frequency={440 + i * 440}:duration=1', '-ac', '1',
                                '-ar', '16000', '-c:a', 'flac', str(dest)], check=True)
                pieces.append(str(dest))
            ref = sync._join_audio(pieces, shutil.which('ffmpeg'), str(folder))
            self.assertEqual(Path(ref).suffix, '.flac')
            self.assertEqual(Path(ref).read_bytes()[:4], b'fLaC')
            out = subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries',
                                           'format=duration', '-of', 'csv=p=0', ref])
            self.assertAlmostEqual(float(out), 2, delta=.1)


if __name__ == '__main__':
    unittest.main()
