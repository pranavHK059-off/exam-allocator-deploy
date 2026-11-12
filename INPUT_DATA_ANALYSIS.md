# Findings from the uploaded 1,000-row sample

## Table sizes and capacity

- Students: 1,000 student-paper entries, split across 10 subject groups and two start times (500 entries at 09:00 and 500 at 13:00).
- Rooms: 28 rooms total; 26 marked `available`, two in maintenance. Available rooms provide 796 usable seats before reserves.
- Default contingency reserve: the allocator holds RM01 (50 usable seats), leaving 746 usable seats per simultaneous sitting.
- Proctors: 30 total, but only eight are available from 08:00; the other 22 start at 11:00. The default reserve holds one broad proctor, leaving seven for the 09:00 sitting.

## Why the original run left entries unassigned

Each of the 10 subjects has 50 candidates at 09:00. The room rule keeps subject groups separate, and each occupied room requires its own eligible proctor. With RM01 held in reserve, only one room (RM17) has 50 seats; the other nine 50-candidate subject groups each need at least two rooms. That is at least 19 simultaneous room bookings, but only seven eligible proctors are available. The room shortage message shown by the earlier UI was misleading: the underlying reason for many unassigned candidates was a staffing shortage.

## Results with the revised allocator

| Configuration | Allocated | Unresolved | Independent audit issues |
|---|---:|---:|---:|
| Default reserve settings and original uploaded tables | 811 / 1,000 | 189 | 0 |
| All three reserve options disabled | 861 / 1,000 | 139 | 0 |
| Simulation with all 30 proctors available from 08:00 | 1,000 / 1,000 | 0 | 0 |

Only change staff availability in a real timetable if those proctors are actually available before 11:00. Otherwise, stagger some subject sittings or review the operational rule that each room booking needs a separate proctor.
