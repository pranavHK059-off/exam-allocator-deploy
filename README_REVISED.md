# Exam Allocation Manager — revised build

## Files

- `app.py` — Flask API, CSV validation, and SQLite-backed table persistence.
- `allocation_engine.py` — subject-pure room allocation, resource-aware proctor assignment, equipment allocation, and independent audit.
- `index.html` — existing web interface, updated to show staffing bottlenecks and the actual reason for an unresolved assignment.
- `requirements.txt` — Python dependencies.
- `exam_allocation_test_datasets.zip` — five synthetic four-table test datasets (provided separately as well).

Keep `app.py`, `allocation_engine.py`, `index.html`, and `requirements.txt` in the same project folder.

## What's changed

1. **Room bundles use least-waste subset-sum.** When eligible proctors are sufficient, the allocator chooses a compatible set of rooms for a subject group that minimizes the number of room bookings first, then unused seats. Rooms are never mixed across subjects/papers in the same time slot.
2. **Scarce staff are spread across groups first.** At a time when the minimum room-booking requirement exceeds eligible staff, the allocator allocates one room per subject group per round before assigning overflow to a second room. This increases the number of students seated under a hard staffing constraint.
3. **The UI reports the actual constraint.** It no longer labels every row with no room assignment as a room-capacity issue when the engine identified a proctor shortage. A separate diagnostic shows the number of room bookings needed versus eligible proctors available.
4. Existing candidate-overlap checks, room/proctor availability rules, protected reserves, atomic equipment allocation, and independent booking audits remain in place.

The allocator is a deterministic heuristic, not a proof of global optimality. It reports shortages rather than pretending an infeasible schedule is fully allocated.

## Finding from the supplied CSVs

The supplied tables contain 1,000 student-paper rows: 500 start at 09:00 and 500 at 13:00. The 28-room table has 26 operational rooms and 796 usable seats before reserves. With the default largest-room reserve (RM01, 50 seats), the active room capacity is 746 seats per sitting.

At 09:00, only eight proctors in the supplied table are available. With one broad proctor held in reserve, seven are usable. There are 10 different subject groups of 50 students. Keeping one subject per room and requiring one proctor per occupied room means at least 19 room bookings are needed to seat the full 500-student sitting: one group can use the remaining 50-seat room, while the other groups need at least two rooms each. Seven proctors cannot supervise 19 simultaneous room bookings.

With the revised round-robin rule, the supplied tables produce **811 allocated rows and 189 unresolved rows** under the default reserves; all 500 rows starting at 13:00 are allocated, and the remaining bottleneck is at 09:00. Disabling all three reserve options improves the count but does not solve the morning staffing shortage.

If it reflects actual staff availability, make enough proctors available from 08:00 to cover the morning sitting. In a local simulation, changing all 30 proctors' `available_from` values to `08:00` produced 1,000/1,000 allocated rows and zero independent-check issues. Do not change staff availability in a real timetable unless those people are genuinely available; otherwise stagger some subject sittings or revise the room/proctor rules.

## Run locally

```bash
python -m pip install -r requirements.txt
python app.py
```

Open `http://127.0.0.1:5000` in Chrome. The UI and Python allocator should be tested via this Flask URL; do not open `index.html` directly with `file://` when testing the complete API integration.

## Test data

Extract `exam_allocation_test_datasets.zip`. Each dataset folder contains exactly these four files: `students.csv`, `rooms.csv`, `proctors.csv`, and `equipment.csv`. Upload all four from the same folder. The test datasets were run directly against `allocation_engine.allocate()` with all default reserve options enabled; expected results are listed in each folder's README and `TEST_REPORT.md`.
