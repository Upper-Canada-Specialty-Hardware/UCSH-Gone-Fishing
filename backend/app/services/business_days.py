from datetime import date, timedelta

from app.services.holidays import is_half_friday, is_company_holiday


def calculate_business_days(
    start_date: date,
    end_date: date,
    holidays: list[dict],
    half_friday_season: tuple[date | None, date | None],
) -> float:
    day_count = 0.0
    current = start_date

    iterations = 0
    while current <= end_date and iterations < 60:
        iterations += 1
        weekday = current.weekday()

        # Skip weekends (5=Saturday, 6=Sunday)
        if weekday in (5, 6):
            current += timedelta(days=1)
            continue

        # Count the weekday
        day_count += 1

        # Half-friday: subtract 0.5
        if is_half_friday(current, half_friday_season):
            day_count -= 0.5

        # Holiday: subtract 1
        is_holiday, _ = is_company_holiday(current, holidays)
        if is_holiday:
            day_count -= 1

        current += timedelta(days=1)

    return day_count
