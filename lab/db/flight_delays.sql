-- US flight delays and cancellations of 2015: every domestic flight of 14 US airlines, from the US Department of
-- Transportation's on-time reports (Bureau of Transportation Statistics), as Maven Analytics' Data Playground publishes
-- them ("Airline Flight Delays"). US government data, in the public domain.
-- Runs once, when the image is built, in a single transaction (psql -1) in the analytics database, into its own schema.
-- Each CSV of the download streams out of the zip into COPY.
--
-- Complete: every row and every value of the files, as they are. The only changes are:
--   * lowercase names, and the types the values have; clock times stay hhmm text, as written;
--   * flights.id, the row's position in flights.csv, since the file has none.
-- October's flights name their airports by the BTS's five-digit numeric airport ids, not by IATA codes like every other
-- month, and no table maps one to the other. So the airport foreign keys are declared NOT VALID: Metabase still sees
-- them, and joins drop October.

\set ON_ERROR_STOP on

CREATE SCHEMA flight_delays;
SET search_path TO flight_delays;

CREATE TABLE airlines (
  iata_code text PRIMARY KEY,
  airline text NOT NULL
);

CREATE TABLE airports (
  iata_code text PRIMARY KEY,
  airport text NOT NULL,
  city text NOT NULL,
  state text NOT NULL,
  country text NOT NULL,
  latitude numeric(8, 5),
  longitude numeric(8, 5)
);

CREATE TABLE cancellation_codes (
  cancellation_reason text PRIMARY KEY,
  cancellation_description text NOT NULL
);

CREATE TABLE flights (
  id integer GENERATED ALWAYS AS IDENTITY,
  year smallint NOT NULL,
  month smallint NOT NULL,
  day smallint NOT NULL,
  day_of_week smallint NOT NULL,
  airline text NOT NULL,
  flight_number integer NOT NULL,
  tail_number text,
  origin_airport text NOT NULL,
  destination_airport text NOT NULL,
  scheduled_departure text NOT NULL,
  departure_time text,
  departure_delay integer,
  taxi_out integer,
  wheels_off text,
  scheduled_time integer,
  elapsed_time integer,
  air_time integer,
  distance integer NOT NULL,
  wheels_on text,
  taxi_in integer,
  scheduled_arrival text NOT NULL,
  arrival_time text,
  arrival_delay integer,
  diverted boolean NOT NULL,
  cancelled boolean NOT NULL,
  cancellation_reason text,
  air_system_delay integer,
  security_delay integer,
  airline_delay integer,
  late_aircraft_delay integer,
  weather_delay integer
);

COPY airlines FROM PROGRAM '7zz e -so /seed/flights.zip airlines.csv' (FORMAT csv, HEADER true);
COPY airports FROM PROGRAM '7zz e -so /seed/flights.zip airports.csv' (FORMAT csv, HEADER true);
COPY cancellation_codes FROM PROGRAM '7zz e -so /seed/flights.zip cancellation_codes.csv' (FORMAT csv, HEADER true);
-- In file order, so id counts the rows.
COPY flights (year, month, day, day_of_week, airline, flight_number, tail_number, origin_airport, destination_airport,
  scheduled_departure, departure_time, departure_delay, taxi_out, wheels_off, scheduled_time, elapsed_time, air_time,
  distance, wheels_on, taxi_in, scheduled_arrival, arrival_time, arrival_delay, diverted, cancelled, cancellation_reason,
  air_system_delay, security_delay, airline_delay, late_aircraft_delay, weather_delay)
  FROM PROGRAM '7zz e -so /seed/flights.zip flights.csv' (FORMAT csv, HEADER true);

-- ---- Keys and indexes, after the load ----------------------------------------------------------------------------------

ALTER TABLE flights
  ALTER id DROP IDENTITY,
  ADD PRIMARY KEY (id),
  ADD FOREIGN KEY (airline) REFERENCES airlines,
  ADD FOREIGN KEY (origin_airport) REFERENCES airports NOT VALID,
  ADD FOREIGN KEY (destination_airport) REFERENCES airports NOT VALID,
  ADD FOREIGN KEY (cancellation_reason) REFERENCES cancellation_codes;

CREATE INDEX ON flights (year, month, day);
CREATE INDEX ON flights (airline);
CREATE INDEX ON flights (origin_airport);
CREATE INDEX ON flights (destination_airport);

-- ---- Descriptions, which Metabase shows --------------------------------------------------------------------------------

COMMENT ON SCHEMA flight_delays IS 'US flight delays and cancellations of 2015: every domestic flight of 14 US airlines, from the US Department of Transportation''s on-time reports, as Maven Analytics publishes them.';

COMMENT ON TABLE airlines IS 'The 14 airlines whose flights are in flights, by IATA code.';
COMMENT ON COLUMN airlines.iata_code IS 'The airline''s two-character IATA code, as in flights.airline.';
COMMENT ON COLUMN airlines.airline IS 'The airline''s name.';

COMMENT ON TABLE airports IS 'The 322 US airports flights go to and from, by IATA code. In October, flights name airports by numeric ids instead, which match none of these.';
COMMENT ON COLUMN airports.iata_code IS 'The airport''s three-letter IATA code, as in flights.origin_airport and flights.destination_airport.';
COMMENT ON COLUMN airports.airport IS 'The airport''s name.';
COMMENT ON COLUMN airports.city IS 'The city the airport serves.';
COMMENT ON COLUMN airports.state IS 'The two-letter code of the airport''s state or territory (AS, GU, PR and VI are territories).';
COMMENT ON COLUMN airports.country IS 'Always USA.';
COMMENT ON COLUMN airports.latitude IS 'Degrees north. Empty for ECP, PBG and UST.';
COMMENT ON COLUMN airports.longitude IS 'Degrees east, so negative. Empty for ECP, PBG and UST.';

COMMENT ON TABLE cancellation_codes IS 'Why a flight was cancelled (flights.cancellation_reason).';
COMMENT ON COLUMN cancellation_codes.cancellation_reason IS 'The code, as in flights.cancellation_reason.';
COMMENT ON COLUMN cancellation_codes.cancellation_description IS 'What the code means.';

COMMENT ON TABLE flights IS 'Every domestic flight of 2015 by these airlines, one row per scheduled flight, including the 89,884 cancelled and 15,187 diverted. Clock times are local to the airport, hhmm on a 24-hour clock, and durations and delays are minutes. In October, origin_airport and destination_airport are numeric airport ids instead of IATA codes, so those flights join no airport.';
COMMENT ON COLUMN flights.id IS 'The row''s position in the source file, from 1: the data has no id of its own.';
COMMENT ON COLUMN flights.year IS 'Year of the flight date, the local date of the scheduled departure: always 2015.';
COMMENT ON COLUMN flights.month IS 'Month of the flight date, 1 to 12.';
COMMENT ON COLUMN flights.day IS 'Day of the month of the flight date.';
COMMENT ON COLUMN flights.day_of_week IS 'Day of the week of the flight date: 1 is Monday, 7 is Sunday.';
COMMENT ON COLUMN flights.airline IS 'The operating airline''s IATA code, see airlines.';
COMMENT ON COLUMN flights.flight_number IS 'The airline''s flight number. Not unique: the same number often flies several legs a day, and other airlines use it too.';
COMMENT ON COLUMN flights.tail_number IS 'The aircraft''s registration. Empty for 14,721 cancelled flights.';
COMMENT ON COLUMN flights.origin_airport IS 'The departure airport''s IATA code, see airports. In October, the BTS''s five-digit numeric airport id instead.';
COMMENT ON COLUMN flights.destination_airport IS 'The scheduled arrival airport''s IATA code, see airports. In October, the BTS''s five-digit numeric airport id instead.';
COMMENT ON COLUMN flights.scheduled_departure IS 'Scheduled departure from the gate, local time, hhmm: 0005 is 00:05.';
COMMENT ON COLUMN flights.departure_time IS 'Actual departure from the gate, local time, hhmm; 2400 is midnight at the end of the day. Empty when the flight never left the gate, for most cancelled flights.';
COMMENT ON COLUMN flights.departure_delay IS 'Minutes from scheduled to actual departure; negative when early.';
COMMENT ON COLUMN flights.taxi_out IS 'Minutes from leaving the gate to wheels off.';
COMMENT ON COLUMN flights.wheels_off IS 'Take-off time, local, hhmm.';
COMMENT ON COLUMN flights.scheduled_time IS 'Scheduled minutes from gate to gate.';
COMMENT ON COLUMN flights.elapsed_time IS 'Actual minutes from gate to gate: taxi_out + air_time + taxi_in. Empty for cancelled and diverted flights.';
COMMENT ON COLUMN flights.air_time IS 'Minutes from wheels off to wheels on. Empty for cancelled and diverted flights.';
COMMENT ON COLUMN flights.distance IS 'Miles between the two airports.';
COMMENT ON COLUMN flights.wheels_on IS 'Landing time, local to the arrival airport, hhmm.';
COMMENT ON COLUMN flights.taxi_in IS 'Minutes from wheels on to arriving at the gate.';
COMMENT ON COLUMN flights.scheduled_arrival IS 'Scheduled arrival at the gate, local to the arrival airport, hhmm.';
COMMENT ON COLUMN flights.arrival_time IS 'Actual arrival at the gate, local to the arrival airport, hhmm; 2400 is midnight.';
COMMENT ON COLUMN flights.arrival_delay IS 'Minutes from scheduled to actual arrival; negative when early. Empty for cancelled and diverted flights. Flights 15 or more minutes late have the five causes of their delay.';
COMMENT ON COLUMN flights.diverted IS 'Whether the flight landed somewhere other than its destination airport.';
COMMENT ON COLUMN flights.cancelled IS 'Whether the flight was cancelled.';
COMMENT ON COLUMN flights.cancellation_reason IS 'Why the flight was cancelled, see cancellation_codes. Set exactly for cancelled flights.';
COMMENT ON COLUMN flights.air_system_delay IS 'Minutes of the arrival delay caused by the National Aviation System: non-extreme weather, airport operations, heavy traffic, air traffic control. The five causes are set only for flights 15 or more minutes late, and add up to arrival_delay.';
COMMENT ON COLUMN flights.security_delay IS 'Minutes of the arrival delay caused by security: evacuations, re-boarding after a breach, long screening lines. Set only for flights 15 or more minutes late.';
COMMENT ON COLUMN flights.airline_delay IS 'Minutes of the arrival delay caused by the airline: maintenance, crew, cleaning, baggage, fueling. Set only for flights 15 or more minutes late.';
COMMENT ON COLUMN flights.late_aircraft_delay IS 'Minutes of the arrival delay caused by the aircraft arriving late from its previous flight. Set only for flights 15 or more minutes late.';
COMMENT ON COLUMN flights.weather_delay IS 'Minutes of the arrival delay caused by extreme weather. Set only for flights 15 or more minutes late.';
