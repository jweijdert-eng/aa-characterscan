"""
CharacterScan Test
"""

# Django
from django.test import TestCase


class TestCharacterScan(TestCase):
    """
    TestCharacterScan
    """

    @classmethod
    def setUpClass(cls) -> None:
        """
        Test setup
        :return:
        :rtype:
        """

        super().setUpClass()

    def test_characterscan(self):
        """
        Dummy test function
        :return:
        :rtype:
        """

        self.assertEqual(True, True)
