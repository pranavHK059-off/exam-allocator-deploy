"""Constraint-aware allocation engine for the Exam Allocation Manager.

This module uses a deterministic, most-constrained-first heuristic. It does not
prove global optimality. All intervals are half-open [start, end), meaning a
booking ending at 10:00 does not conflict with one starting at 10:00.

The Flask layer validates uploaded records; the helpers here also validate the
numeric/time values they consume so a direct call fails clearly rather than
silently creating an invalid allocation.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable


NONE_VALUES = {"", "none", "n/a", "na", "null"}
TRUE_VALUES = {"1", "true", "yes", "y", "required"}


def _normal(value: Any) -> str:
    """Return a trimmed, case-insensitive comparison key."""
    if value is None:
        return ""
    return str(value).strip().casefold()


def _minute(value: Any) -> int:
    raw = str(value).strip()
    parts = raw.split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid time: {value!r}; expected HH:MM.")
    try:
        hour, minute = (int(part) for part in parts)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid time: {value!r}; expected HH:MM.") from exc
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError(f"Invalid time: {value!r}; expected a 24-hour time.")
    return hour * 60 + minute


def _integer(value: Any, field: str, *, minimum: int = 0) -> int:
    """Parse whole-number values such as 60 and 60.0 consistently."""
    if value is None or str(value).strip() == "":
        value = 0 if minimum == 0 else None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a whole number.") from exc
    if not number.is_integer() or number < minimum:
        qualifier = "zero or greater" if minimum == 0 else f"at least {minimum}"
        raise ValueError(f"{field} must be a whole number {qualifier}.")
    return int(number)


def _overlap(start: int, end: int, other_start: int, other_end: int) -> bool:
    return start < other_end and other_start < end


def _bool(value: Any) -> bool:
    return _normal(value) in TRUE_VALUES


def _taught(proctor: dict) -> set[str]:
    raw = str(proctor.get("subjects_taught", ""))
    return {
        _normal(part)
        for part in raw.replace(";", "|").replace(",", "|").split("|")
        if _normal(part) not in NONE_VALUES
    }


def _usable_capacity(room: dict) -> int:
    capacity = _integer(room.get("capacity", 0), "room capacity")
    unusable = _integer(room.get("unusable_seats", 0) or 0, "unusable_seats")
    if unusable > capacity:
        raise ValueError(f"Room {room.get('room_id', '')!r} has more unusable seats than capacity.")
    return capacity - unusable


def _time_fits(resource: dict, start: int, end: int) -> bool:
    return _minute(resource["available_from"]) <= start and end <= _minute(resource["available_to"])


def _append_reason(item: dict, code: str) -> None:
    if code not in item["reason_codes"]:
        item["reason_codes"].append(code)


def _equipment_request(row: dict) -> list[str]:
    base = _normal(row.get("equipment", "none"))
    requested = [] if base in NONE_VALUES else [base]
    if _bool(row.get("reader")):
        requested.append("reader")
    if _bool(row.get("scribe")):
        requested.append("scribe")
    return list(dict.fromkeys(requested))


def _equipment_scarcity(row: dict, stock: dict[str, int], reserve: dict[str, int]) -> float:
    pressure = 0.0
    for kind in _equipment_request(row):
        available = max(0, stock.get(kind, 0) - reserve.get(kind, 0))
        pressure += 1.0 / max(1, available)
    return pressure


def _free_for_interval(bookings: Iterable[dict], day: str, start: int, end: int) -> bool:
    return not any(
        booking["date"] == day and _overlap(start, end, booking["start"], booking["end"])
        for booking in bookings
    )


def _equipment_can_fit(
    bookings: list[dict], day: str, start: int, end: int, limit: int
) -> bool:
    """Check peak concurrent use, not merely the number of overlapping bookings."""
    if limit <= 0:
        return False
    events: list[tuple[int, int]] = []
    for booking in bookings:
        if booking["date"] != day or not _overlap(start, end, booking["start"], booking["end"]):
            continue
        clipped_start = max(start, booking["start"])
        clipped_end = min(end, booking["end"])
        events.append((clipped_start, 1))
        events.append((clipped_end, -1))
    active = 0
    # End events precede start events at the same time, matching [start, end).
    for _time, delta in sorted(events, key=lambda event: (event[0], event[1])):
        active += delta
        if active >= limit:
            return False
    return True


def allocate(
    students: list[dict],
    rooms: list[dict],
    proctors: list[dict],
    equipment: list[dict],
    options: dict | None = None,
) -> dict[str, Any]:
    """Allocate candidate-paper rows to rooms, proctors, and equipment.

    Optional student columns recognised when supplied are ``separate_room``,
    ``required_room_type``, ``reader``, ``scribe``, and ``rest_break_min``.
    Reader/scribe consume equipment inventory only when requested, matching
    stock exists, and availability is sufficient for the complete interval.
    """
    options = options if isinstance(options, dict) else {}
    reserve_room = _bool_option(options.get("reserve_room", True))
    reserve_proctor = _bool_option(options.get("reserve_proctor", True))
    reserve_equipment = _bool_option(options.get("reserve_equipment", True))

    equipment_stock: dict[str, int] = {}
    for row in equipment:
        kind = _normal(row.get("equipment_type"))
        if not kind:
            continue
        quantity = _integer(row.get("quantity", 0), f"quantity for equipment {kind!r}")
        equipment_stock[kind] = quantity
    equipment_reserve = {
        kind: (1 if reserve_equipment and quantity > 1 else 0)
        for kind, quantity in equipment_stock.items()
    }

    # Build and validate normalized operational-resource views.
    room_capacity: dict[str, int] = {}
    operational_rooms: list[dict] = []
    for room in rooms:
        rid = str(room.get("room_id", "")).strip()
        capacity = _usable_capacity(room)
        room_capacity[rid] = capacity
        if _normal(room.get("status", "available")) == "available" and capacity > 0:
            # Validate resource times here so malformed direct calls fail clearly.
            _minute(room["available_from"])
            _minute(room["available_to"])
            operational_rooms.append(room)
    operational_rooms.sort(key=lambda room: (-room_capacity[str(room.get("room_id", "")).strip()], str(room.get("room_id", ""))))

    # Reserve the largest operational room as the most flexible contingency room.
    reserved_room = operational_rooms[0] if reserve_room and len(operational_rooms) > 1 else None
    usable_rooms = [room for room in operational_rooms if room is not reserved_room]

    for proctor in proctors:
        _minute(proctor["available_from"])
        _minute(proctor["available_to"])
    all_subjects = {_normal(row.get("subject")) for row in students if _normal(row.get("subject"))}
    broad_proctors = [person for person in proctors if not (_taught(person) & all_subjects)]
    broad_proctors.sort(
        key=lambda person: (
            -(_minute(person["available_to"]) - _minute(person["available_from"])),
            str(person.get("proctor_id", "")),
        )
    )
    reserved_proctor = broad_proctors[0] if reserve_proctor and len(broad_proctors) > 1 else None
    usable_proctors = [person for person in proctors if person is not reserved_proctor]

    entries: list[dict] = []
    by_candidate: dict[str, list[int]] = defaultdict(list)
    for index, source in enumerate(students):
        row = dict(source)
        row["date"] = str(row.get("date", "")).strip()
        start = _minute(row["start_time"])
        duration = _integer(row.get("duration_min"), "duration_min", minimum=1)
        extra = _integer(row.get("extra_time_min", 0) or 0, "extra_time_min")
        rest = _integer(row.get("rest_break_min", 0) or 0, "rest_break_min")
        end = start + duration + extra + rest
        if end > 24 * 60:
            raise ValueError(f"Candidate {row.get('candidate_id', '')!r} has a sitting that extends past midnight.")

        requested = _equipment_request(row)
        item = {
            **row,
            "_index": index,
            "start_minute": start,
            "end_minute": end,
            "room_id": None,
            "room_capacity": 0,
            "room_total_capacity": 0,
            "room_unusable_seats": 0,
            "seat": None,
            "proctor_id": None,
            "proctor_name": "Unassigned",
            "equipment_available": not requested,
            "equipment_status": "not_required" if not requested else "pending",
            "equipment_allocations": [],
            "missing_equipment": [],
            "candidate_conflict": False,
            "reason_codes": [],
            "assignment_status": "unassigned",
            "_requested_equipment": requested,
        }
        entries.append(item)
        by_candidate[_normal(row.get("candidate_id"))].append(index)

    # Sweep by candidate and date. Keep every conflicting row unassigned.
    for indexes in by_candidate.values():
        indexes.sort(key=lambda i: (entries[i]["date"], entries[i]["start_minute"], entries[i]["end_minute"]))
        for pos, left_i in enumerate(indexes):
            left = entries[left_i]
            for right_i in indexes[pos + 1 :]:
                right = entries[right_i]
                if right["date"] != left["date"]:
                    if right["date"] > left["date"]:
                        break
                    continue
                if right["start_minute"] >= left["end_minute"]:
                    break
                if _overlap(left["start_minute"], left["end_minute"], right["start_minute"], right["end_minute"]):
                    left["candidate_conflict"] = True
                    right["candidate_conflict"] = True
                    _append_reason(left, "student_overlap")
                    _append_reason(right, "student_overlap")

    # A session is a common paper at one starting time. Access requirements split
    # it further into room-compatible groups; separate-room candidates are alone.
    sessions: dict[tuple[str, str, str, int], list[dict]] = defaultdict(list)
    for item in entries:
        if not item["candidate_conflict"]:
            key = (item["date"], _normal(item.get("subject")), _normal(item.get("paper")), item["start_minute"])
            sessions[key].append(item)

    allocation_groups: list[dict[str, Any]] = []
    for session, rows in sessions.items():
        by_requirement: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for item in rows:
            separate = _bool(item.get("separate_room"))
            required_type = _normal(item.get("required_room_type"))
            if separate:
                allocation_groups.append({"session": session, "rows": [item], "separate": True, "room_type": required_type})
            else:
                by_requirement[("shared", required_type)].append(item)
        for (_mode, room_type), group_rows in by_requirement.items():
            allocation_groups.append({"session": session, "rows": group_rows, "separate": False, "room_type": room_type})

    room_bookings: dict[str, list[dict]] = defaultdict(list)
    staff_bookings: dict[str, list[dict]] = defaultdict(list)
    equipment_bookings: dict[str, list[dict]] = defaultdict(list)

    def compatible_rooms(group: dict[str, Any]) -> list[dict]:
        required_type = group["room_type"]
        if not required_type:
            return usable_rooms
        return [room for room in usable_rooms if _normal(room.get("room_type")) == required_type]

    def eligible_staff(subject: str, day: str, start: int, end: int) -> list[dict]:
        result = []
        for person in usable_proctors:
            pid = str(person.get("proctor_id", "")).strip()
            if subject in _taught(person) or not _time_fits(person, start, end):
                continue
            if not _free_for_interval(staff_bookings.get(pid, []), day, start, end):
                continue
            result.append(person)
        # Use the least-flexible valid person first, preserving broad coverage.
        result.sort(key=lambda person: (
            _minute(person["available_to"]) - _minute(person["available_from"]),
            str(person.get("proctor_id", "")),
        ))
        return result

    def candidate_priority(item: dict) -> tuple:
        return (
            -_equipment_scarcity(item, equipment_stock, equipment_reserve),
            -item["end_minute"],
            _normal(item.get("candidate_id")),
        )

    def static_group_priority(group: dict[str, Any]) -> tuple:
        session = group["session"]
        rows = group["rows"]
        day, subject, _paper, start = session
        row_room_counts = []
        row_staff_counts = []
        for item in rows:
            end = item["end_minute"]
            row_room_counts.append(sum(1 for room in compatible_rooms(group) if _time_fits(room, start, end)))
            row_staff_counts.append(len(eligible_staff(subject, day, start, end)))
        equipment_pressure = sum(_equipment_scarcity(item, equipment_stock, equipment_reserve) for item in rows)
        return (
            min(row_room_counts, default=0),
            min(row_staff_counts, default=0),
            0 if group["separate"] else 1,
            group["room_type"],
            -equipment_pressure,
            -len(rows),
            day,
            start,
            subject,
            _normal(rows[0].get("paper")) if rows else "",
        )

    allocation_groups.sort(key=static_group_priority)

    def _best_room_bundle(
        candidates: list[dict], demand: int, max_rooms: int
    ) -> list[dict] | None:
        """Find a low-waste set of rooms that can seat a whole group.

        Room capacity is the number of students from this one subject/group that
        can be seated in that room. The score first minimizes the number of room
        bookings (and therefore proctors), then minimizes unused seats, with room
        IDs providing deterministic tie-breaking.
        """
        if demand <= 0 or max_rooms <= 0 or not candidates:
            return None

        ordered = sorted(candidates, key=lambda r: str(r.get("room_id", "")))
        # (total capacity, room count) -> deterministic subset attaining it.
        states: dict[tuple[int, int], tuple[dict, ...]] = {(0, 0): ()}
        for room in ordered:
            room_id = str(room.get("room_id", "")).strip()
            capacity = room_capacity[room_id]
            updated = dict(states)
            for (total, count), chosen_rooms in states.items():
                if count >= max_rooms:
                    continue
                proposed = chosen_rooms + (room,)
                key = (total + capacity, count + 1)
                current = updated.get(key)
                proposed_ids = tuple(sorted(str(r.get("room_id", "")) for r in proposed))
                current_ids = tuple(sorted(str(r.get("room_id", "")) for r in current)) if current else ()
                if current is None or proposed_ids < current_ids:
                    updated[key] = proposed
            states = updated

        covering = [
            (count, total - demand, tuple(sorted(str(r.get("room_id", "")) for r in chosen)), chosen)
            for (total, count), chosen in states.items()
            if count and total >= demand
        ]
        if not covering:
            return None
        return list(min(covering, key=lambda value: (value[0], value[1], value[2]))[3])

    # Estimate how many simultaneously occupied rooms a complete sitting needs.
    # This is used to identify staff-constrained start times before greedily
    # consuming a second proctor for one subject while other subjects get none.
    groups_by_start: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for group in allocation_groups:
        day, _subject, _paper, start = group["session"]
        groups_by_start[(day, start)].append(group)

    staffing_diagnostics: list[dict[str, Any]] = []
    staff_shortage_starts: set[tuple[str, int]] = set()

    def _single_room_options(group: dict[str, Any]) -> list[dict]:
        day, _subject, _paper, start = group["session"]
        end = max(item["end_minute"] for item in group["rows"])
        demand = len(group["rows"])
        return [
            room for room in compatible_rooms(group)
            if room_capacity[str(room.get("room_id", "")).strip()] >= demand
            and _time_fits(room, start, end)
        ]

    def _maximum_single_room_matching(groups: list[dict[str, Any]]) -> set[int]:
        """Maximum matching of groups to distinct rooms that fit each whole group."""
        options_by_group = {
            id(group): sorted(
                _single_room_options(group),
                key=lambda room: (room_capacity[str(room.get("room_id", "")).strip()], str(room.get("room_id", ""))),
            )
            for group in groups
        }
        matching: dict[str, int] = {}

        def visit(group_id: int, seen: set[str]) -> bool:
            for room in options_by_group[group_id]:
                room_id = str(room.get("room_id", "")).strip()
                if room_id in seen:
                    continue
                seen.add(room_id)
                if room_id not in matching or visit(matching[room_id], seen):
                    matching[room_id] = group_id
                    return True
            return False

        for group in sorted(groups, key=lambda g: (len(options_by_group[id(g)]), -len(g["rows"]), str(g["session"]))):
            visit(id(group), set())
        return set(matching.values())

    for (day, start), groups_at_start in groups_by_start.items():
        groups_at_start = sorted(groups_at_start, key=static_group_priority)
        matched_single = _maximum_single_room_matching(groups_at_start)
        # Every group needs at least one room. Groups not matched to a distinct
        # single-room solution need a second room; add a conservative correction
        # for unusually large groups that need three or more rooms.
        minimum_room_bookings = len(groups_at_start) + (len(groups_at_start) - len(matched_single))
        for group in groups_at_start:
            if id(group) in matched_single:
                continue
            compatible = compatible_rooms(group)
            capacity_sum = 0
            individual_minimum = 0
            demand = len(group["rows"])
            for capacity in sorted(
                (room_capacity[str(room.get("room_id", "")).strip()] for room in compatible),
                reverse=True,
            ):
                capacity_sum += capacity
                individual_minimum += 1
                if capacity_sum >= demand:
                    break
            if capacity_sum < demand:
                # If even all compatible room seats cannot cover this group, it
                # remains a room constraint regardless of available staffing.
                individual_minimum = max(individual_minimum, len(compatible) + 1)
            if individual_minimum > 2:
                minimum_room_bookings += individual_minimum - 2

        eligible_union: dict[str, dict] = {}
        for group in groups_at_start:
            _d, subject, _p, group_start = group["session"]
            earliest_end = min(item["end_minute"] for item in group["rows"])
            for person in usable_proctors:
                pid = str(person.get("proctor_id", "")).strip()
                if subject in _taught(person):
                    continue
                if _minute(person["available_from"]) <= group_start and _minute(person["available_to"]) >= earliest_end:
                    eligible_union[pid] = person

        available_staff = len(eligible_union)
        if available_staff < minimum_room_bookings:
            staff_shortage_starts.add((day, start))
            staffing_diagnostics.append({
                "date": day,
                "start_time": f"{start // 60:02d}:{start % 60:02d}",
                "subjects": sorted({str(group["rows"][0].get("subject", group["session"][1])) for group in groups_at_start}),
                "minimum_room_bookings_for_full_sitting": minimum_room_bookings,
                "eligible_proctors_available_at_start": available_staff,
                "shortfall": minimum_room_bookings - available_staff,
                "message": (
                    f"At least {minimum_room_bookings} occupied-room proctor bookings are needed "
                    f"to seat every subject group at {start // 60:02d}:{start % 60:02d}; "
                    f"only {available_staff} eligible proctors are available at that time. "
                    "Keep subjects separate by room: add proctors whose availability covers this sitting "
                    "or stagger some subjects."
                ),
            })

    # At a staff-constrained start time, take one room for each group per round
    # before giving any one subject a second room. This avoids using most of the
    # small morning staff pool on overflow seats for only a few subjects.
    # When staffing is sufficient, process each group fully and use subset-sum
    # room bundles to minimize empty seats and unnecessary proctor bookings.
    worklist: list[dict[str, Any]] = []
    max_rounds = max(1, len(usable_rooms))
    for round_index in range(max_rounds):
        for group in allocation_groups:
            day, _subject, _paper, start = group["session"]
            if (day, start) in staff_shortage_starts:
                worklist.append(group)
            elif round_index == 0:
                worklist.append(group)

    room_bookings: dict[str, list[dict]] = defaultdict(list)
    staff_bookings: dict[str, list[dict]] = defaultdict(list)
    equipment_bookings: dict[str, list[dict]] = defaultdict(list)
    blocked_groups: set[int] = set()

    for group_info in worklist:
        group_identity = id(group_info)
        if group_identity in blocked_groups:
            continue
        session = group_info["session"]
        day, subject, paper, start = session
        group_rows = group_info["rows"]
        remaining = sorted(
            [item for item in group_rows if item["room_id"] is None],
            key=candidate_priority,
        )
        if not remaining:
            continue
        rooms_for_group = compatible_rooms(group_info)
        round_robin = (day, start) in staff_shortage_starts

        while remaining:
            best: tuple[tuple[Any, ...], dict, list[dict], dict] | None = None
            thresholds = sorted({item["end_minute"] for item in remaining}, reverse=True)

            # If all remaining candidates have the same actual end time, first
            # select a low-waste room bundle capable of seating the whole group.
            # In a staff-shortage round, do not monopolize extra proctors: assign
            # just one room for this group and allow other groups their first room.
            bundle_room_ids: set[str] | None = None
            if not round_robin and len({item["end_minute"] for item in remaining}) == 1:
                common_end = remaining[0]["end_minute"]
                free_for_bundle = [
                    room for room in rooms_for_group
                    if _time_fits(room, start, common_end)
                    and _free_for_interval(
                        room_bookings.get(str(room.get("room_id", "")).strip(), []),
                        day, start, common_end,
                    )
                ]
                staff_for_bundle = eligible_staff(subject, day, start, common_end)
                bundle = _best_room_bundle(free_for_bundle, len(remaining), len(staff_for_bundle))
                if bundle:
                    bundle_room_ids = {str(room.get("room_id", "")).strip() for room in bundle}

            for room in rooms_for_group:
                rid = str(room.get("room_id", "")).strip()
                if bundle_room_ids is not None and rid not in bundle_room_ids:
                    continue
                capacity = room_capacity[rid]
                if capacity <= 0 or _minute(room["available_from"]) > start:
                    continue
                for threshold in thresholds:
                    if threshold > _minute(room["available_to"]):
                        continue
                    if not _free_for_interval(room_bookings.get(rid, []), day, start, threshold):
                        continue
                    pool = [item for item in remaining if item["end_minute"] <= threshold]
                    if not pool:
                        continue
                    ending_here = [item for item in pool if item["end_minute"] == threshold]
                    others = sorted((item for item in pool if item["end_minute"] != threshold), key=candidate_priority)
                    chosen = (ending_here + others)[:capacity]
                    if not chosen:
                        continue
                    actual_end = max(item["end_minute"] for item in chosen)
                    staff_options = eligible_staff(subject, day, start, actual_end)
                    if not staff_options:
                        continue
                    staff = staff_options[0]
                    score = (
                        -len(chosen),
                        capacity - len(chosen),
                        _minute(staff["available_to"]) - _minute(staff["available_from"]),
                        rid,
                        str(staff.get("proctor_id", "")),
                    )
                    if best is None or score < best[0]:
                        best = (score, room, chosen, staff)

            if best is None:
                for item in remaining:
                    end = item["end_minute"]
                    possible_rooms = [
                        room for room in rooms_for_group
                        if _time_fits(room, start, end)
                        and _free_for_interval(
                            room_bookings.get(str(room.get("room_id", "")).strip(), []), day, start, end
                        )
                    ]
                    if not possible_rooms:
                        _append_reason(item, "room_capacity_or_availability")
                    elif not eligible_staff(subject, day, start, end):
                        _append_reason(item, "no_eligible_proctor_for_room")
                    else:
                        _append_reason(item, "room_capacity_or_availability")
                        _append_reason(item, "no_eligible_proctor_for_room")
                blocked_groups.add(group_identity)
                break

            _score, room, chosen, person = best
            rid = str(room.get("room_id", "")).strip()
            pid = str(person.get("proctor_id", "")).strip()
            booking_end = max(item["end_minute"] for item in chosen)
            booking = {
                "date": day,
                "start": start,
                "end": booking_end,
                "room": room,
                "session": session,
                "seats": [],
                "proctor": person,
            }
            room_bookings[rid].append(booking)
            staff_bookings[pid].append({
                "date": day,
                "start": start,
                "end": booking_end,
                "room_id": rid,
                "subject": subject,
                "session": session,
            })

            # Equipment requests are atomic: candidates never receive only part
            # of a multi-item request.
            for seat_no, item in enumerate(chosen, start=1):
                item["room_id"] = room.get("room_id")
                item["room_capacity"] = room_capacity[rid]
                item["room_total_capacity"] = _integer(room.get("capacity", 0), "capacity")
                item["room_unusable_seats"] = _integer(room.get("unusable_seats", 0) or 0, "unusable_seats")
                item["seat"] = seat_no
                item["proctor_id"] = person.get("proctor_id")
                item["proctor_name"] = person.get("proctor_name", str(person.get("proctor_id", "Unassigned")))
                booking["seats"].append(item["_index"])

                requested = item["_requested_equipment"]
                if not requested:
                    item["equipment_available"] = True
                    item["equipment_status"] = "not_required"
                    continue

                limits = {
                    kind: max(0, equipment_stock.get(kind, 0) - equipment_reserve.get(kind, 0))
                    for kind in requested
                }
                possible = all(
                    _equipment_can_fit(
                        equipment_bookings[kind], day, item["start_minute"], item["end_minute"], limits[kind]
                    )
                    for kind in requested
                )
                if possible:
                    for kind in requested:
                        equipment_bookings[kind].append({
                            "date": day,
                            "start": item["start_minute"],
                            "end": item["end_minute"],
                            "candidate_id": item.get("candidate_id"),
                        })
                    item["equipment_allocations"] = requested[:]
                    item["equipment_available"] = True
                    item["equipment_status"] = "assigned"
                else:
                    item["equipment_allocations"] = []
                    item["missing_equipment"] = [
                        kind for kind in requested
                        if not _equipment_can_fit(
                            equipment_bookings[kind], day, item["start_minute"], item["end_minute"], limits[kind]
                        )
                    ]
                    item["equipment_available"] = False
                    item["equipment_status"] = "quantity_shortage"
                    _append_reason(item, "equipment_quantity")

            chosen_ids = {id(item) for item in chosen}
            remaining = [item for item in remaining if id(item) not in chosen_ids]
            if round_robin:
                # Give each subject group one booking before opening its next room.
                break

    # Independent audit: the allocation code and audit deliberately use separate
    # passes so accidental overlaps/capacity errors are surfaced for review.
    audit: list[dict[str, Any]] = []
    room_lookup = {str(room.get("room_id", "")).strip(): room for room in rooms}
    proctor_lookup = {str(person.get("proctor_id", "")).strip(): person for person in proctors}

    for room_id, bookings in room_bookings.items():
        room = room_lookup[room_id]
        for index, booking in enumerate(bookings):
            if len(booking["seats"]) > _usable_capacity(room):
                audit.append({"resource": room_id, "issue": "capacity_exceeded"})
            if _normal(room.get("status", "available")) != "available":
                audit.append({"resource": room_id, "issue": "room_not_operational"})
            if not _time_fits(room, booking["start"], booking["end"]):
                audit.append({"resource": room_id, "issue": "outside_room_availability"})
            for other in bookings[index + 1 :]:
                if booking["date"] == other["date"] and _overlap(booking["start"], booking["end"], other["start"], other["end"]):
                    audit.append({"resource": room_id, "issue": "overlapping_room_bookings"})

    for proctor_id, bookings in staff_bookings.items():
        person = proctor_lookup[proctor_id]
        for booking in bookings:
            if not _time_fits(person, booking["start"], booking["end"]):
                audit.append({"resource": proctor_id, "issue": "outside_proctor_availability"})
            if booking["subject"] in _taught(person):
                audit.append({"resource": proctor_id, "issue": "proctor_teaches_exam_subject"})
        for index, left in enumerate(bookings):
            for right in bookings[index + 1 :]:
                if left["date"] == right["date"] and _overlap(left["start"], left["end"], right["start"], right["end"]):
                    audit.append({"resource": proctor_id, "issue": "overlapping_proctor_duties"})

    for kind, bookings in equipment_bookings.items():
        limit = max(0, equipment_stock.get(kind, 0) - equipment_reserve.get(kind, 0))
        events_by_day: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for booking in bookings:
            events_by_day[booking["date"]].extend([(booking["start"], 1), (booking["end"], -1)])
        for day, events in events_by_day.items():
            active = 0
            for _time, delta in sorted(events, key=lambda event: (event[0], event[1])):
                active += delta
                if active > limit:
                    audit.append({"resource": kind, "issue": "equipment_quantity_exceeded", "date": day})
                    break

    for item in entries:
        if item["candidate_conflict"]:
            item["room_id"] = None
            item["room_capacity"] = 0
            item["room_total_capacity"] = 0
            item["room_unusable_seats"] = 0
            item["seat"] = None
            item["proctor_id"] = None
            item["proctor_name"] = "Unassigned"
            item["equipment_available"] = not item["_requested_equipment"]
            item["equipment_status"] = "not_allocated_candidate_conflict" if item["_requested_equipment"] else "not_required"
            item["equipment_allocations"] = []
        elif item["room_id"] is None:
            if item["_requested_equipment"]:
                item["equipment_status"] = "not_allocated_room"
            item["assignment_status"] = "unassigned"
        elif item["proctor_id"] is not None and item["equipment_available"]:
            item["assignment_status"] = "allocated"
        else:
            item["assignment_status"] = "partial"

        if item["candidate_conflict"]:
            item["assignment_status"] = "unassigned"
        item["room_reason"] = "room_capacity_or_availability" if "room_capacity_or_availability" in item["reason_codes"] else None
        item["proctor_reason"] = "no_eligible_proctor_for_room" if "no_eligible_proctor_for_room" in item["reason_codes"] else None
        item.pop("_requested_equipment", None)
        item.pop("_index", None)

    unresolved = sum(item["assignment_status"] != "allocated" for item in entries)
    meta = {
        "reserve": {
            "room": (
                {
                    "room_id": reserved_room.get("room_id"),
                    "capacity": room_capacity[str(reserved_room.get("room_id", "")).strip()],
                    "total_capacity": _integer(reserved_room.get("capacity", 0), "capacity"),
                    "unusable_seats": _integer(reserved_room.get("unusable_seats", 0) or 0, "unusable_seats"),
                }
                if reserved_room else None
            ),
            "proctor": ({"proctor_id": reserved_proctor.get("proctor_id"), "proctor_name": reserved_proctor.get("proctor_name")} if reserved_proctor else None),
            "equipment": equipment_reserve,
        },
        "rooms_used": sum(1 for bookings in room_bookings.values() if bookings),
        "operational_rooms": len(operational_rooms),
        "room_bookings": sum(len(bookings) for bookings in room_bookings.values()),
        "proctor_bookings": sum(len(bookings) for bookings in staff_bookings.values()),
        "proctors_used": sum(1 for bookings in staff_bookings.values() if bookings),
        "independent_check_issues": audit,
        "staffing_diagnostics": staffing_diagnostics,
        "method": "deterministic most-constrained-first heuristic; low-waste subset-sum room bundles for subject-pure groups; round-robin room allocation when proctors are the bottleneck; one proctor per room booking; atomic candidate-specific equipment allocation; independent room, proctor, and equipment audit",
        "limitations": [
            "This heuristic does not prove that an unresolved allocation is impossible.",
            "A contingency reserve can cause avoidable shortages; regenerate with reserve options disabled to test capacity.",
            "Room-type and separate-room requirements are applied when the optional student columns are supplied.",
        ],
    }
    return {
        "entries": entries,
        "entry_count": len(entries),
        "unresolved_count": unresolved,
        "status": "review" if unresolved or audit else "allocated",
        "meta": meta,
    }


def _bool_option(value: Any) -> bool:
    """Parse booleans without treating the string 'false' as truthy."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    return _normal(value) in TRUE_VALUES
