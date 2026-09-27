"""Repository indexing secret-safety pins (red-team Pass-2 finding 3)."""
import tempfile
import unittest
from pathlib import Path

from bug_hunter import repository


class TestRepositorySecretSafety(unittest.TestCase):
    def test_dotfiles_and_secret_patterns_never_indexed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'ok.py').write_text('x = 1\n', encoding='utf-8')
            (root / 'notes.txt').write_text('hello\n', encoding='utf-8')
            (root / '.env').write_text('TOKEN=secret\n', encoding='utf-8')
            (root / '.npmrc').write_text('//registry:_auth=x\n', encoding='utf-8')
            (root / 'id_rsa').write_text('PRIVATE KEY\n', encoding='utf-8')
            (root / 'id_ed25519.pub').write_text('ssh-ed25519 A\n',
                                                encoding='utf-8')
            (root / 'cert.pem').write_text('PRIVATE\n', encoding='utf-8')
            (root / 'server.key').write_text('PRIVATE\n', encoding='utf-8')
            (root / 'credentials.json').write_text('{"k":1}\n', encoding='utf-8')
            (root / '.git').mkdir()
            (root / '.git' / 'config').write_text('[core]\n', encoding='utf-8')
            (root / '.github').mkdir()
            (root / '.github' / 'ci.yml').write_text('on: push\n', encoding='utf-8')
            idx = repository.ProjectIndex.read(root)
            rels = sorted(e.relpath for e in idx.entries)
            self.assertEqual(rels, ['notes.txt', 'ok.py'])

    def test_walk_stops_when_file_cap_hit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for i in range(5):
                (root / f'f{i}.txt').write_text('x\n' * 10, encoding='utf-8')
            idx = repository.ProjectIndex.read(root, max_files=2)
            self.assertLessEqual(len(idx.entries), 2)
            self.assertTrue(idx.limited)

    def test_plain_and_hidden_are_counted_not_invisible(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'ok.py').write_text('x = 1\n', encoding='utf-8')
            (root / '.env').write_text('T=1\n', encoding='utf-8')
            idx = repository.ProjectIndex.read(root)
            self.assertEqual(idx.files_considered, 2)
            self.assertEqual(idx.skipped_binary_or_invalid, 0)
            self.assertEqual(idx.skipped_secret, 1)
            self.assertFalse(idx.limited)


if __name__ == '__main__':
    unittest.main()
