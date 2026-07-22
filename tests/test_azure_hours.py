import app as app_module


def _time_range(date, start_h, start_m, end_h, end_m):
    return {
        'startTime': {'date': date, 'hour': start_h, 'minute': start_m},
        'endTime': {'date': date, 'hour': end_h, 'minute': end_m},
    }


def test_azure_opening_hours_returns_none_without_data():
    assert app_module._azure_opening_hours({}) is None
    assert app_module._azure_opening_hours({'openingHours': {'timeRanges': []}}) is None


def test_azure_opening_hours_maps_date_to_spanish_weekday():
    # 2026-07-20 is a Monday.
    poi = {'openingHours': {'timeRanges': [_time_range('2026-07-20', 10, 0, 22, 0)]}}
    assert app_module._azure_opening_hours(poi) == ['Lunes: 10:00–22:00']


def test_azure_opening_hours_combines_split_ranges_on_the_same_day():
    poi = {'openingHours': {'timeRanges': [
        _time_range('2026-07-20', 9, 0, 14, 0),
        _time_range('2026-07-20', 16, 0, 20, 0),
    ]}}
    assert app_module._azure_opening_hours(poi) == ['Lunes: 09:00–14:00, 16:00–20:00']


def test_azure_opening_hours_covers_multiple_days_sorted_by_weekday():
    poi = {'openingHours': {'timeRanges': [
        _time_range('2026-07-21', 10, 0, 22, 0),  # Tuesday
        _time_range('2026-07-20', 10, 0, 22, 0),  # Monday
    ]}}
    assert app_module._azure_opening_hours(poi) == ['Lunes: 10:00–22:00', 'Martes: 10:00–22:00']


def test_azure_opening_hours_result_feeds_normalize_parse_hours():
    import normalize
    poi = {'openingHours': {'timeRanges': [_time_range('2026-07-20', 10, 0, 22, 0)]}}
    hours = app_module._azure_opening_hours(poi)
    schedule = normalize.parse_hours(hours)
    assert schedule[0] == [(600, 1320)]  # Monday == day index 0
