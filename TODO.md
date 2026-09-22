# TODO

## Map Visualization
- [ ] Display fixed schematic map of all rail lines and Flatiron Flyer (FF) routes.
    - [X] Evaluate Beck/Vignelli-style schematic vs. MapBox/Leaflet for implementation.
    - [X] Show vehicle icons (rail cars, FF bus) moving along lines in real time.
    - [X] Outline icons by frequency: green (≤15 min), orange (≤30 min), red (≥60 min), etc.
    - [X] Update icon outlines dynamically as frequencies change by time/day.
    - [X] On icon click, show dialog with:
        - [X] Next stop
        - [X] On-time/late status (+/- x mins)

## Real-Time & Historical Data
- [ ] Store and serve historical vehicle position and timing data.
- [ ] Build UI to view historical performance for each line/vehicle.
- [ ] Allow download/export of historical data (CSV/JSON).
- [ ] Calculate and display on-time performance and other stats.

## Traffic Integration (FF Bus)
Still wanted, but this is a big one. Budget a dedicated block of work rather than
squeezing it in alongside smaller changes.
- [ ] Integrate Google Maps traffic API for FF buses.
    - [ ] Estimate arrival times at stops using current bus position and traffic data.

## Unofficial Alerts
**Dropped.** Stuck-vehicle detection was removed entirely — see Known Fixes below.
- [X] ~~Detect and display alerts for vehicles stuck >5 minutes.~~
    - [X] ~~Integrate with Transit App API to confirm stuck status when GTFS-RT is unreliable.~~

## Event & Game Awareness
Surface local sporting events so riders can anticipate delays and crowds.
Shipped for sports via ESPN's unofficial scoreboard API — see
`backend/app/services/sports.py` and `frontend/lib/gameSlides.ts`.
- [X] Pull home-game schedules and live game state from a free sports API.
    - [X] **Home games only.** Away games don't move Denver ridership.
    - [X] Show nothing at all on days with no home game.
    - [X] Before the game: show start time (e.g. "7pm MT"). Inside one hour, switch
      to a countdown ("COL game in 45 min").
    - [X] During the game: show where play is at, e.g. "top 7th" or "2nd period".
    - [X] After the game: show the result and how long ago it ended ("COL won 20 min
      ago"), then clear the slide once it's two hours old.
- [X] Present it inside the existing "x vehicles" widget as a rotating carousel, so
  several same-day games each get a turn.
    - [X] Colour each slide in that team's colours (Broncos orange, Rockies purple,
      and so on).
    - [X] Lead each slide with an emoji identifying the sport.
- [X] Tracked clubs: Avalanche, Nuggets, Rockies, Broncos, Rapids, Summit.
- [X] Password-gated `/sim` page that scripts a live game server-side, so the
  whole pre → live → final → cleared arc can be checked on a real device in a
  few minutes. Always expires back to the ESPN feed.
- [ ] Verify Denver Summit FC's brand hexes. The club hasn't published them, so
  `services/sports.py` carries an approximation of the green/gold crest and
  defers to whatever ESPN reports — which needs a look once they're playing.
- [ ] Later: extend the same widget to non-sports events (concerts, conventions,
  festivals). There's no good free feed for these, so it likely depends on
  community-submitted entries.

## Dashboard & Analytics
- [ ] Build dashboard for:
    - [ ] On-time performance (priority)
    - [ ] Frequency stats
    - [ ] Delay incidents
    - [ ] (Brainstorm more: e.g., average trip time, most delayed segments, headway adherence, missed stops, etc.)

## Backend
- [ ] Refactor GTFS-RT ingestion to persist data in DB.
- [ ] Build REST/WebSocket API for:
    - [ ] Real-time vehicle positions
    - [ ] Historical data queries
    - [ ] Frequency calculations
    - [ ] Alerts and stats

## Frontend
- [ ] Implement map and dashboard UI.
- [ ] Connect to backend APIs for real-time and historical data.
- [ ] Responsive design for desktop/mobile.

## Testing & Reliability
- [ ] Add unit/integration tests for backend and frontend.
- [ ] Monitor GTFS-RT feed reliability; add fallback/alerting for outages.

## User Accounts
- [X] No user accounts or authentication (explicitly out of scope).

## Known Fixes
- [X] Outline the selected vehicle in white to improve visibility against the map background.
- [X] Remove vehicles with no average headway from Current Frequency list on the Dashboard
- [X] Vehicle stuck logic: **feature removed.** Repeated reworks never made it honest — the endpoint and off-route guards each fixed one class of false positive and exposed another, and the alert count stayed higher than was believable. Two root causes, neither fixable from our side: the GTFS-RT feed repeats stale positions, and a vehicle legitimately holding at a stop (operator recovery time, restroom break) is indistinguishable from a broken-down one. A feed that can't tell "stopped" from "stuck" can't support the feature, so it's gone rather than permanently wrong. The Dashboard's KPI slot now shows **Service Delivered %** and the bottom card shows **Service Delivery by Route** (both already computed by `/analytics/overview` and `/analytics/service-delivery`).
- [X] Remove duplicate "On time" tag from the Vehicle Dialog box
- [X] Rework On-time logic to be more accurate
- [X] Include link to schedule on RTD website in Vehicle Dialog box
- [X] Include link to Greater Denver Transit blog posts on each bus and train line on the dialog box
- [X] Add Github repo button in the top right corner of the page
- [X] Don't show vehicles at Union Station (39.7531695197791, -105.00028537059949) until a certain zoom level is reached, since they are often stuck there and clutter the map
- [X] Add search functionality to the map to allow users to search for a specific vehicle by its ID or route number. Make sure when the search feature is added that it works well on mobile devices, since the map will be used on both desktop and mobile (you may need to use a hamburger menu or a collapsible search bar on mobile to save space).
- [X] Update the 101C and 101T to be train lines since these are new service being run by RTD. Instead of 101C and 101T call them C and T, and update the icons to be train cars instead of buses. Also update the colors of the lines to match RTD's branding for these lines (C Line is #f79239, T Line is #b71318).
- [X] Add stations to the map when you click on a vehicle, so that users can see where the vehicle is going and what stops it will make. The stations should be displayed as small circles on the map, with the station name displayed when you hover over the circle. The stations should also be color-coded to match the line that the vehicle is on (e.g., C Line stations should be orange, T Line stations should be red).
- [X] Add padding to the Stuck Vehicle Alert time tag on the row so that the See on Map icon doesn't move when the time changes
- [X] Change the bus and train icons to look a bit more modern and visually appealing. The current icons are pretty basic and could be improved to make the map look nicer.
- [X] Max zoom out to the entire Denver metro area, so that users can't zoom out to the point where the map is just a tiny dot in the middle of the screen.
- [X] Fix: The on-time tag in the Vehicle Dialog box isn't corresponding to the status in the Dashboard. Namely the stuck status, but also if it's running early or behind schedule. The tag should indicate whether a vehicle is on time, stuck, early, or late, and should say how early or late the vehicle is (only if its early or late by +- 5 minutes)
- [X] Fix: The search functionality needs some tweaking as it isn't able to pull up the trains by typing in "Route [x]".
- [X] Feat: Add station dialog boxes with more information about a station. The stations will still not appear on the map like they do now, but you can search for a station and click on it to see more information about it. The dialog box should include the station name, the lines that stop at the station, the next arrival times for each line, and any other relevant information. The dialog box should also include a link to the RTD website for more information about the station (RTD formats the link as so: https://app.rtd-denver.com/nextride/stop/{stop ID}).
- [X] Feat: from the dashboard, I want to be able to drill down into individual vehicles (ex FF #1505) by clicking on the timeslot and seeing all the vehicles that were active for that timeslot. Then, when you click on the vehicle from that page, you'll be presented with a line showing the vehicle's route and all the stops it made, along with the times it arrived at each stop and other stats like occupancy, delay status, etc. This will allow users to see the performance of individual vehicles in more detail and identify any patterns or issues with specific vehicles.
- [X] Feat: add search to drop down menu on the Dashboard.
- [X] Feat: change the Historical page to a Trips page that you see from the Dashboard page when you click on the On-Time Performance Trend widget or When Is Service Reliable widget. Current URI is [url]/dashboard/vehicles?[time-contraint], but I want that to change to [url]/trips?[time-constaint]. I want the dashboard to then link to the Trips page with the time constraint that the user clicked on from the widget. Still allow users to be able to download raw data. I want there to be a user-interactable calendar and time selector on the Trips page so that users can select a specific date and time range to view trips for. I also want the user to be able to filter by line/route. Essentially I want the page to be just like the /dashboard/vehicles page, but with the changes I described above.