import logging
import os
import time
import traceback

import numpy
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS
from bme280 import BME280
from smbus2 import SMBus
from ltr559 import LTR559


logging.basicConfig(
    format="%(asctime)s.%(msecs)03d %(levelname)-8s %(message)s",
    level=logging.INFO,
    datefmt="%Y-%m-%d %H:%M:%S")

logging.info("""compensated-temperature.py - Use the CPU temperature
to compensate temperature readings from the BME280 sensor.
Method adapted from Initial State's Enviro pHAT review:
https://medium.com/@InitialState/tutorial-review-enviro-phat-for-raspberry-pi-4cd6d8c63441

Press Ctrl+C to exit!

""")

def get_cpu_temperature():
    with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
        temp = f.read()
        temp = int(temp) / 1000.0
    return temp

class InfluxDBCollector:
    def __init__(self, host, token, org, bucket):
        self.client = InfluxDBClient(url=host, token=token, org=org, bucket=bucket)
        self.bucket = bucket
        self.write_api = self.client.write_api(write_options=SYNCHRONOUS)
        self.default_tags = {}

    def add_default_tags(self, tags):
        self.default_tags.update(tags)

    def write_point_value(self, point, field, value, tags=None):
        point = Point(point).field(field, value)
        if tags:
            for key, value in tags.items():
                point = point.tag(key, value) 
        for key, value in self.default_tags.items():
            point = point.tag(key, value)
        self.write_api.write(bucket=self.bucket, record=point)


class Monitor:
    def __init__(self, influxdb_client):
        self.influxdb_client = influxdb_client
        self.bus = SMBus(1)
        self.bme280 = BME280(i2c_dev=self.bus)
        self.ltr559 = LTR559()

        # Pressure variables
        self.pressure_vals = []
        self.time_vals = []
        self.num_vals = 1000
        self.interval = 1
        self.trend = "-"

        self.cpu_temps = [get_cpu_temperature()] * 5

        self.factor = 2.25


    def get_temperature(self) -> tuple[float, float]:
        cpu_temp = get_cpu_temperature()
        self.cpu_temps = self.cpu_temps[1:] + [cpu_temp]
        avg_cpu_temp = sum(self.cpu_temps) / float(len(self.cpu_temps))
        raw_temp = self.bme280.get_temperature()
        comp_temp = raw_temp - ((avg_cpu_temp - raw_temp) / self.factor)
        return comp_temp, raw_temp

    def correct_humidity(self, humidity, temperature, corr_temperature) -> float:
        dewpoint = temperature - ((100 - humidity) / 5)
        corr_humidity = 100 - (5 * (corr_temperature - dewpoint))
        return float(min(100, corr_humidity))

    def get_humidity(self, corrected_temp, raw_temp) -> float:
        humidity = self.bme280.get_humidity()
        dewpoint = raw_temp - ((100 - humidity) / 5)
        corr_humidity = 100 - (5 * (corrected_temp - dewpoint))
        return float(min(100, corr_humidity))

    def analyse_pressure(self, pressure) -> tuple[float, float, str]:
        t = time.time()
        if len(self.pressure_vals) > self.num_vals:
            self.pressure_vals = self.pressure_vals[1:] + [pressure]
            self.time_vals = self.time_vals[1:] + [t]
            
            line = numpy.polyfit(self.time_vals, self.pressure_vals, 1, full=True)

            # Calculate slope, variance, and confidence
            slope = line[0][0]
            intercept = line[0][1]
            variance = numpy.var(self.pressure_vals)
            residuals = numpy.var([(slope * x + intercept - y) for x, y in zip(self.time_vals, self.pressure_vals)])
            r_squared = 1 - residuals / variance

            # Calculate change in pressure per hour
            change_per_hour = slope * 60 * 60
            mean_pressure = numpy.mean(self.pressure_vals)

            # Calculate trend
            if r_squared > 0.5:
                if change_per_hour > 0.5:
                    self.trend = ">"
                elif change_per_hour < -0.5:
                    self.trend = "<"
                elif -0.5 <= change_per_hour <= 0.5:
                    self.trend = "-"

                if self.trend != "-":
                    if abs(change_per_hour) > 3:
                        self.trend *= 2
        else:
            self.pressure_vals.append(pressure)
            self.time_vals.append(t)
            mean_pressure = numpy.mean(self.pressure_vals)
            change_per_hour = 0
            self.trend = "-"

        return (mean_pressure, change_per_hour, self.trend)

    def get_light(self) -> float:
        return self.ltr559.get_lux()

    def get_pressure(self) -> float:
        pressure = self.bme280.get_pressure()
        mean_pressure, change_per_hour, trend = self.analyse_pressure(pressure)
        return mean_pressure
    
    def collect(self):
        corrected_temp, raw_temp = self.get_temperature()
        self.influxdb_client.write_point_value('room_temperature', 'temperature', corrected_temp)

        humidity = self.get_humidity(corrected_temp, raw_temp)
        self.influxdb_client.write_point_value('room_humidity', 'humidity', humidity)

        light = self.get_light()
        self.influxdb_client.write_point_value('room_light', 'light', light)

        pressure = self.get_pressure()
        self.influxdb_client.write_point_value('room_pressure', 'pressure', pressure)
        t = time.time()
        print("{} - {}C - {}% - {}lux - {}Pa".format(t, corrected_temp, humidity, light, pressure))
        
        
if __name__ == '__main__':
    influxdb_url = os.environ.get("INFLUXDB_URL")
    influxdb_token = os.environ.get("INFLUXDB_TOKEN")
    influxdb_org = os.environ.get("INFLUXDB_ORG")
    influxdb_bucket = os.environ.get("INFLUXDB_BUCKET")

    room_name = os.environ.get("ROOM_NAME")

    influxdb_client = InfluxDBCollector(host=influxdb_url, token=influxdb_token, org=influxdb_org, bucket=influxdb_bucket)
    influxdb_client.add_default_tags({"room": room_name})

    monitor = Monitor(influxdb_client)    

    while True:
        try:
            monitor.collect() 
        except Exception as e:
            print(e)
            traceback.print_exc()
        finally:
            ms = time.time() % 1
            time.sleep(1.0 - ms)
