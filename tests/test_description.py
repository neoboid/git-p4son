"""Tests for git_p4son.description."""

import unittest

from git_p4son.description import Description, number_commits


class TestParse(unittest.TestCase):
    def test_message_and_commits(self):
        d = Description.parse(
            ['Fix the login bug', '1. Add validation', '2. Fix redirect'])
        self.assertEqual(d.message, ['Fix the login bug'])
        self.assertEqual(d.commits, ['Add validation', 'Fix redirect'])
        self.assertEqual(d.trailing, [])

    def test_no_commits(self):
        d = Description.parse(['Just a message'])
        self.assertEqual(d.message, ['Just a message'])
        self.assertEqual(d.commits, [])

    def test_trailing_text_after_commits(self):
        d = Description.parse(
            ['Message', '1. First', '2. Second', 'Trailing note'])
        self.assertEqual(d.commits, ['First', 'Second'])
        self.assertEqual(d.trailing, ['Trailing note'])

    def test_empty_description(self):
        self.assertEqual(Description.parse([]), Description())

    def test_multiline_message_before_commits(self):
        d = Description.parse(['Line 1', 'Line 2', '1. Commit one'])
        self.assertEqual(d.message, ['Line 1', 'Line 2'])
        self.assertEqual(d.commits, ['Commit one'])

    def test_numbered_list_in_user_message_not_mistaken_for_commits(self):
        """A numbered list in the user's message is not mistaken for the commit list."""
        d = Description.parse([
            'Reasons for this change:',
            '1. performance',
            '2. simplicity',
            '',
            'Changes included:',
            '1. Add cache',
            '2. Remove old path',
        ])
        self.assertEqual(d.message, ['Reasons for this change:',
                                     '1. performance', '2. simplicity'])
        self.assertEqual(d.commits, ['Add cache', 'Remove old path'])

    def test_keywords_come_out_of_message_and_trailing(self):
        d = Description.parse(['Title', '', '#review-1', 'Changes included:',
                               '1. A', 'Note', '#review-1', '#review-2'])
        self.assertEqual(d.message, ['Title'])
        self.assertEqual(d.trailing, ['Note'])
        self.assertEqual(d.keywords, ['#review-1', '#review-2'])

    def test_keyword_inside_text_stays(self):
        """Only a keyword on a line of its own is Swarm's; #reviewer is not one."""
        d = Description.parse(['See #review notes', '#reviewer'])
        self.assertEqual(d.message, ['See #review notes', '#reviewer'])
        self.assertEqual(d.keywords, [])


class TestFormat(unittest.TestCase):
    def test_layout(self):
        d = Description(['Title'], ['A', 'B'], ['Note'], ['#review-1'])
        self.assertEqual(d.format(), [
            'Title', '', 'Changes included:', '1. A', '2. B', 'Note', '',
            '#review-1'])

    def test_message_only(self):
        self.assertEqual(Description(['Title']).format(), ['Title'])

    def test_round_trip(self):
        lines = ['Title', '', 'Body', '', 'Changes included:', '1. A',
                 '2. B', '', '#review-1']
        self.assertEqual(Description.parse(lines).format(), lines)


class TestEditing(unittest.TestCase):
    def test_set_message_takes_its_keyword(self):
        d = Description(['Old'], keywords=['#review-1'])
        d.set_message('New\n\n#review-1')
        self.assertEqual(d.message, ['New'])
        self.assertEqual(d.keywords, ['#review-1'])

    def test_merge_replaces_covered_commits_and_appends(self):
        d = Description(commits=['Add validation', 'Fix redirect'])
        d.merge_commits(['Fix redirect', 'New commit'])
        self.assertEqual(d.commits,
                         ['Add validation', 'Fix redirect', 'New commit'])

    def test_repeated_subjects_replace_one_for_one(self):
        d = Description(commits=['Fix', 'Fix', 'Other'])
        d.merge_commits(['Fix'])
        self.assertEqual(d.commits, ['Fix', 'Other', 'Fix'])

    def test_number_commits(self):
        self.assertEqual(number_commits(['A', 'B']), ['1. A', '2. B'])


if __name__ == '__main__':
    unittest.main()
