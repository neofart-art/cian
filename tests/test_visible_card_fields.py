import unittest
from collector.card_fields import normalize_browser_card


class VisibleCardFields(unittest.TestCase):
    def card(self, lines, **extra):
        return normalize_browser_card(dict(
            url='https://www.cian.ru/sale/flat/336618602/',
            title=lines[0], text='\n'.join(lines), **extra))

    def test_marketing_title(self):
        item = self.card([
            'Квартира у метро,юрид.чистая',
            '1-комн. квартира, 38,1 м², 5/14 этаж',
            'Только на Циан', 'Проверено в Росреестре',
            'Братиславская', '7 минут пешком',
            'Москва, ЮВАО, р-н Марьино, Братиславская улица, 6К1',
            '14 500 000 ₽'], metro='Братиславская')
        self.assertEqual(item['title'], '1-комн. квартира, 38,1 м², 5/14 этаж')
        self.assertEqual(item['area'], 38.1)
        self.assertEqual(item['floor'], '5/14')
        self.assertEqual(item['metro_walk_minutes'], 7)

    def test_missing_time_label(self):
        item = self.card([
            'Тихий уголок для вашей жизни!',
            '1-комн. квартира, 41,6 м², 13/14 этаж',
            'Хорошая цена', 'Новопеределкино', '10 минут пешком',
            'Москва, ЗАО, улица Скульптора Мухиной, 7К1',
            '15 000 000 ₽'], metro='Новопеределкино')
        self.assertEqual(item['area'], 41.6)
        self.assertEqual(item['metro_walk_minutes'], 10)

    def test_station_time_pair_wins_over_address_station_and_prose(self):
        item = self.card([
            '1-комн. квартира, 38,2 м², 15/16 этаж',
            'Хорошая цена', 'Выхино', '13 минут пешком',
            'Москва, ЮВАО, м. Юго-Восточная, Сормовская улица, 8К2',
            '13 800 000 ₽', 'До метро Юго-Восточная 11 минут пешком'],
            metro='Юго-Восточная', metro_time='11 мин. пешком')
        self.assertEqual(item['metro'], 'Выхино')
        self.assertEqual(item['metro_walk_minutes'], 13)

    def test_seller_typo_does_not_override_header(self):
        item = self.card([
            '1-комн. квартира, 35,5 м², 9/12 этаж',
            'Домодедовская', '9 минут пешком',
            'Москва, Домодедовская улица, 22К1', '14 900 000 ₽',
            'Две станции метро в 78 минутах пешком'],
            metro='Домодедовская')
        self.assertEqual(item['metro_walk_minutes'], 9)

    def test_unknown_stays_unknown(self):
        item = self.card([
            'Отличное предложение!', 'Новопеределкино', '10 мин.',
            'Москва, улица Скульптора Мухиной', '15 000 000 ₽',
            '1-комн. квартира, 100 м², 1/2 этаж',
            'До метро 5 минут пешком'],
            metro='Новопеределкино', metro_time='5 минут пешком')
        self.assertNotIn('metro_time', item)
        self.assertNotIn('area', item)
        self.assertEqual(item['title'], 'Отличное предложение!')

    def test_transport_is_not_walking(self):
        item = self.card([
            '1-комн. квартира, 38 м², 2/14 этаж',
            'Люблино', '10 минут транспортом',
            'Москва, Капотня', '12 000 000 ₽'], metro='Люблино')
        self.assertEqual(item['metro'], 'Люблино')
        self.assertEqual(item['metro_time'], '10 мин. транспортом')
        self.assertIsNone(item['metro_walk_minutes'])

    def test_recommendation_flag_preserved(self):
        item = self.card(['Квартира'], search_section='recommendation')
        self.assertEqual(item['search_section'], 'recommendation')


if __name__ == '__main__':
    unittest.main()
