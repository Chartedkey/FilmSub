from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from filmsub import embedded
from filmsub.config import Settings
from filmsub.gui_audio import subtitle_label
from filmsub.mkv import SubTrack
from filmsub.srt import Cue, read_srt, write_srt
from filmsub.translator import FileTranslator, load_source, output_path_for


class SourceSelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.movie = str(self.root / 'Film.mkv')
        self.settings = Settings()
        self.tracks = [SubTrack(2, 'S_TEXT/UTF8', 'eng', 'Full'),
                       SubTrack(3, 'S_TEXT/ASS', 'fra', 'French'),
                       SubTrack(4, 'S_HDMV/PGS', 'eng', 'Pictures'),
                       SubTrack(5, 'S_TEXT/UTF8', 'rus')]

    def test_select_french_text_and_filter_images(self):
        chooser = Mock(return_value=3)
        cues = [Cue(1000, 2000, 'Bonjour !')]
        with patch.object(embedded, 'list_tracks', return_value=self.tracks), patch.object(embedded, 'extract', return_value=cues) as extract:
            source = load_source(self.movie, self.settings, Mock(), choose_subtitles=chooser)
        self.assertEqual([t.number for t in chooser.call_args.args[0]], [2, 3, 5])
        self.assertIs(extract.call_args.args[1], self.tracks[1])
        self.assertEqual((source.language, source.cues, source.output), ('fra', cues, output_path_for(self.movie)))

    def test_sidecar_remains_selectable(self):
        side = self.root / 'Film.en.srt'
        write_srt(str(side), [Cue(1000, 2000, 'Hello there!')])
        with patch.object(embedded, 'list_tracks', return_value=self.tracks), patch.object(embedded, 'extract') as extract:
            source = load_source(self.movie, self.settings, Mock(), choose_subtitles=lambda _: -1)
        self.assertEqual(source.cues[0].text, 'Hello there!')
        extract.assert_not_called()

    def test_cancel_before_extraction_or_api(self):
        with patch.object(embedded, 'list_tracks', return_value=self.tracks), patch.object(embedded, 'extract') as extract, patch('filmsub.translator.ChatClient') as client:
            translator = FileTranslator(self.settings, Mock(), choose_subtitles=lambda _: None)
            with self.assertRaises(embedded.SubtitleCancelled):
                translator.translate(self.movie)
            extract.assert_not_called()
            client.return_value.chat_retry.assert_not_called()
        self.assertFalse(Path(output_path_for(self.movie)).exists())

    def test_graphical_only_has_clear_message(self):
        log, choose = Mock(), Mock()
        with patch.object(embedded, 'list_tracks', return_value=[self.tracks[2]]):
            self.assertIsNone(load_source(self.movie, self.settings, log, choose_subtitles=choose))
        choose.assert_not_called()
        self.assertIn('OCR', log.call_args.args[0])

    def test_no_subtitles(self):
        log, choose = Mock(), Mock()
        with patch.object(embedded, 'list_tracks', return_value=[]):
            self.assertIsNone(load_source(self.movie, self.settings, log, choose_subtitles=choose))
        self.assertIn('нет встроенных', log.call_args.args[0])
        choose.assert_not_called()

    def test_russian_source_not_sent_for_translation(self):
        with patch.object(embedded, 'list_tracks', return_value=self.tracks), patch.object(embedded, 'extract') as extract:
            self.assertIsNone(load_source(self.movie, self.settings, Mock(), choose_subtitles=lambda _: 5))
        extract.assert_not_called()

    def test_direct_srt_keeps_existing_behavior(self):
        side = self.root / 'Film.en.srt'
        write_srt(str(side), [Cue(1000, 2000, 'Hello there!')])
        chooser = Mock()
        source = load_source(str(side), self.settings, Mock(), choose_subtitles=chooser)
        self.assertEqual(source.cues[0].text, 'Hello there!')
        chooser.assert_not_called()

    def test_english_prompt_unchanged_and_french_prompt_adjusted(self):
        translator = FileTranslator.__new__(FileTranslator)
        prompt = 'Переводи английские субтитры. Поле en.'
        translator._source_language = 'eng'
        self.assertEqual(translator._source_prompt(prompt), prompt)
        translator._source_language = 'fra'
        adjusted = translator._source_prompt(prompt)
        self.assertNotIn('английские субтитры', adjusted)
        self.assertIn('fra', adjusted)

    def test_labels_include_language_and_forced(self):
        track = SubTrack(3, 'S_TEXT/UTF8', 'jpn', 'Signs', forced=True)
        self.assertIn('японский', subtitle_label(track))
        self.assertIn('forced', subtitle_label(track))


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'requires ffmpeg')
class EmbeddedIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.en = self.root / 'en.srt'
        self.fr = self.root / 'fr.srt'
        write_srt(str(self.en), [Cue(1200, 2500, 'Hello there!'), Cue(3000, 4300, 'Good morning!')], encoding='utf-8')
        write_srt(str(self.fr), [Cue(1200, 2500, 'Bonjour !'), Cue(3000, 4300, 'Merci beaucoup.')], encoding='utf-8')

    def movie(self, extension, codec):
        movie = self.root / ('Film.' + extension)
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(self.en), '-i', str(self.fr),
                        '-map', '0:s', '-map', '1:s', '-c:s', codec,
                        '-metadata:s:s:0', 'language=eng', '-metadata:s:s:1', 'language=fra',
                        '-metadata:s:s:1', 'title=French', str(movie)], check=True)
        return str(movie)

    def verify_french(self, movie):
        tracks = embedded.list_tracks(movie)
        self.assertEqual(len(tracks), 2)
        selected = next(t for t in tracks if t.language == 'fra')
        cues = embedded.extract(movie, selected)
        self.assertEqual([c.text for c in cues], ['Bonjour !', 'Merci beaucoup.'])
        self.assertEqual([(c.start_ms, c.end_ms) for c in cues], [(1200, 2500), (3000, 4300)])
        source = load_source(movie, Settings(), Mock(), choose_subtitles=lambda _: selected.number)
        self.assertEqual(source.language, 'fra')
        self.assertEqual([c.text for c in source.cues], ['Bonjour !', 'Merci beaucoup.'])

    def test_native_mkv_srt_selection(self):
        self.verify_french(self.movie('mkv', 'srt'))

    def test_native_mkv_ass_selection(self):
        self.verify_french(self.movie('mkv', 'ass'))

    def test_mp4_mov_text_selection(self):
        self.verify_french(self.movie('mp4', 'mov_text'))

    def test_mp4_selection_uses_global_stream_index(self):
        movie = str(self.root / 'WithVideo.mp4')
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i',
                        'color=size=32x32:duration=5:rate=1', '-i', str(self.en), '-i', str(self.fr),
                        '-map', '0:v', '-map', '1:s', '-map', '2:s', '-c:v', 'mpeg4', '-c:s', 'mov_text',
                        '-metadata:s:s:0', 'language=eng', '-metadata:s:s:1', 'language=fra', movie], check=True)
        tracks = embedded.list_tracks(movie)
        self.assertEqual([t.number for t in tracks], [1, 2])
        self.assertEqual(embedded.extract(movie, tracks[1])[0].text, 'Bonjour !')

    def test_selected_embedded_source_produces_russian_sidecar(self):
        movie = self.movie('mkv', 'srt')
        before = Path(movie).read_bytes()
        selected = next(t for t in embedded.list_tracks(movie) if t.language == 'fra')
        settings = Settings(analyze_film=False, use_glossary=False)
        def translate_batch(system, cues, *args):
            self.assertEqual([c.text for c in cues], ['Bonjour !', 'Merci beaucoup.'])
            self.assertIn('fra', system)
            for cue, russian in zip(cues, ['Здравствуйте!', 'Большое спасибо.']):
                cue.ru = russian
        with patch('filmsub.translator.app_dir', return_value=str(self.root)), patch('filmsub.translator.ChatClient') as client:
            translator = FileTranslator(settings, Mock(), choose_subtitles=lambda _: selected.number)
            with patch.object(translator, '_run_parallel', side_effect=translate_batch):
                result = translator.translate(movie)
            client.return_value.chat_retry.assert_not_called()
        self.assertEqual(result['status'], 'done')
        cues = read_srt(output_path_for(movie))
        self.assertEqual([c.text for c in cues], ['Здравствуйте!', 'Большое спасибо.'])
        self.assertEqual([(c.start_ms, c.end_ms) for c in cues], [(1200, 2500), (3000, 4300)])
        self.assertEqual(Path(movie).read_bytes(), before)

    def test_stop_before_extraction(self):
        movie = self.movie('mkv', 'srt')
        with self.assertRaises(embedded.SubtitleCancelled):
            embedded.extract(movie, embedded.list_tracks(movie)[0], stop_flag=lambda: True)


if __name__ == '__main__':
    unittest.main()
