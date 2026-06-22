from GtkHelper.GtkHelper import ComboRow
from src.backend.PluginManager.ActionBase import ActionBase
from src.backend.PluginManager.PluginBase import PluginBase
from src.backend.PluginManager.ActionHolder import ActionHolder
from src.backend.DeckManagement.InputIdentifier import Input, InputEvent
from src.backend.PluginManager.ActionInputSupport import ActionInputSupport

# Import gtk modules
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw

import sys
import os
import time
from PIL import Image, ImageDraw, ImageFont
from loguru import logger as log
import requests
from threading import Timer
import datetime

# Add plugin to sys.paths
sys.path.append(os.path.dirname(__file__))

# Import globals
import globals as gl


# Normalization helper functions
def owm_to_wmo(code):
    if 200 <= code < 300:
        return 95  # Thunderstorm
    elif 300 <= code < 400:
        return 51  # Drizzle
    elif code == 500 or code == 501:
        return 61  # Light rain
    elif 502 <= code <= 504:
        return 65  # Heavy rain
    elif code == 511:
        return 66  # Freezing rain
    elif 520 <= code <= 531:
        return 80  # Rain showers
    elif code == 600 or code == 601:
        return 71  # Snow fall
    elif code == 602:
        return 75  # Heavy snow
    elif code == 611 or code == 612:
        return 77  # Sleet / Snow grains
    elif 620 <= code <= 622:
        return 85  # Snow showers
    elif 701 <= code <= 781:
        return 45  # Fog
    elif code == 800:
        return 0  # Clear sky
    elif code == 801:
        return 1  # Mainly clear
    elif code == 802:
        return 2  # Partly cloudy
    elif code == 803 or code == 804:
        return 3  # Overcast
    return 0


def twc_to_wmo(code):
    mapping = {
        0: 95, 1: 95, 2: 95, 3: 95, 4: 95,
        5: 66, 6: 66, 7: 77, 8: 56, 9: 51,
        10: 66, 11: 80, 12: 80, 13: 71, 14: 85,
        15: 75, 16: 73, 17: 96, 18: 77, 19: 45,
        20: 45, 21: 45, 22: 45, 23: 0, 24: 0,
        25: 0, 26: 3, 27: 2, 28: 2, 29: 1,
        30: 1, 31: 0, 32: 0, 33: 0, 34: 0,
        35: 96, 36: 0, 37: 95, 38: 95, 39: 80,
        40: 65, 41: 85, 42: 75, 43: 75, 45: 80,
        46: 85, 47: 95
    }
    return mapping.get(code, 0)


def is_rain_or_snow(code):
    if code is None:
        return False
    return (
        51 <= code <= 57 or
        61 <= code <= 67 or
        71 <= code <= 77 or
        80 <= code <= 86 or
        95 <= code <= 99
    )


class WindDirection(ActionBase):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.show_interval = 30  # minutes
        self.show_timer: Timer = None
        self.cached_wind = None
        self.last_fetch_time = None
        
    def on_ready(self):
        self.show()

    def get_config_rows(self) -> list:
        self.units_model = Gtk.ListStore.new([str, int])
        self.units_row = ComboRow(title=self.plugin_base.lm.get("actions.unit.title"), model=self.units_model)
        self.lat_entry = Adw.EntryRow(title=self.plugin_base.lm.get("actions.lat-entry.title"), input_purpose=Gtk.InputPurpose.NUMBER)
        self.lon_entry = Adw.EntryRow(title=self.plugin_base.lm.get("actions.long-entry.title"), input_purpose=Gtk.InputPurpose.NUMBER)

        self.units_cell_renderer = Gtk.CellRendererText()
        self.units_row.combo_box.pack_start(self.units_cell_renderer, True)
        self.units_row.combo_box.add_attribute(self.units_cell_renderer, "text", 0)

        self.load_units_model()
        self.load_config_defaults()

        # Connect signals
        self.lat_entry.connect("notify::text", self.on_lat_changed)
        self.lon_entry.connect("notify::text", self.on_lon_changed)
        self.units_row.combo_box.connect("changed", self.on_units_changed)

        return [self.lat_entry, self.lon_entry, self.units_row]
    
    def load_units_model(self):
        self.units_model.append([self.plugin_base.lm.get("actions.units.metric"), 1])
        self.units_model.append([self.plugin_base.lm.get("actions.units.imperial"), 2])
    
    def on_lat_changed(self, entry, text):
        settings = self.get_settings()
        settings["lat"] = entry.get_text()
        self.set_settings(settings)
        self.show(force=True)
    
    def on_lon_changed(self, entry, *args):
        settings = self.get_settings()
        settings["lon"] = entry.get_text()
        self.set_settings(settings)
        self.show(force=True)

    def on_units_changed(self, combo_box, *args):
        unit = self.units_model[combo_box.get_active()][1]
        settings = self.get_settings()
        settings["unit"] = unit
        self.set_settings(settings)
        self.show(force=True)

    def load_config_defaults(self):
        settings = self.get_settings()
        self.lat_entry.set_text(settings.get("lat", ""))  # Does not accept None
        self.lon_entry.set_text(settings.get("lon", ""))  # Does not accept None

        if settings.get("unit") == 2:  # Imperial
            self.units_row.combo_box.set_active(1)
        else:  # Celsius and none
            self.units_row.combo_box.set_active(0)

    def get_wind_data(self, force=False) -> list[float]:
        now_time = time.time()
        if not force and self.cached_wind is not None and self.last_fetch_time is not None:
            if now_time - self.last_fetch_time < self.show_interval * 60:
                return self.cached_wind

        settings = self.get_settings()
        lat = settings.get("lat")
        lon = settings.get("lon")
        imperial = settings.get("unit") == 2

        try:
            lat = float(lat)
            lon = float(lon)
        except (TypeError, ValueError):
            return None

        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": lat,
            "longitude": lon,
            "current": ["wind_speed_10m", "wind_direction_10m"]
        }

        if imperial:
            params["wind_speed_unit"] = "mph"

        try:
            resp = requests.get(url, params=params, timeout=5)
            if resp.status_code != 200:
                return None
            data = resp.json()
        except Exception as e:
            log.error(e)
            return None

        result = [
            data["current"]["wind_direction_10m"],
            data["current"]["wind_speed_10m"],
            data["current_units"]["wind_speed_10m"]
        ]
        self.cached_wind = result
        self.last_fetch_time = now_time
        return result
    
    def show(self, force=False):
        if not self.get_is_present():
            return
        # Stop timer if active
        if self.show_timer is not None:
            self.show_timer.cancel()

        wind_data = self.get_wind_data(force=force)
        if wind_data is None:
            self.show_error()
            return
        
        wind_direction, wind_speed, wind_speed_unit = wind_data

        self.set_bottom_label(f"{int(wind_speed)} {wind_speed_unit}", font_size=12)

        icon_path = self.plugin_base.get_icon_path("wind_direction")
        try:
            with Image.open(icon_path) as img:
                image = img.copy()
            image = image.rotate(wind_direction, expand=True)
            self.set_media(image=image, size=0.85, valign=-1)
        except Exception as e:
            log.error(f"Error drawing wind icon: {e}")
            self.show_error()

        # Launch timer
        self.show_timer = Timer(self.show_interval * 60, self.show)
        self.show_timer.start()

    def on_key_down(self):
        self.show(force=True)
    
    def get_custom_config_area(self):
        return Gtk.Label(label=self.plugin_base.lm.get("actions.open-meteo-thanks"))


class Weather(ActionBase):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.show_interval = 30  # minutes
        self.show_timer: Timer = None
        self.display_page = 0
        self.page_timer: Timer = None
        self.cached_weather = None
        self.last_fetch_time = None
        self.icon_cache = {}
        self.init_fonts()
        
    def init_fonts(self):
        font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        try:
            self.font_large = ImageFont.truetype(font_path, 28)
            self.font_medium = ImageFont.truetype(font_path, 12)
            self.font_title = ImageFont.truetype(font_path, 10)
            self.font_text = ImageFont.truetype(font_path, 8)
            self.font_button_temp = ImageFont.truetype(font_path, 16)
            self.font_button_loc = ImageFont.truetype(font_path, 9)
        except Exception:
            self.font_large = ImageFont.load_default()
            self.font_medium = ImageFont.load_default()
            self.font_title = ImageFont.load_default()
            self.font_text = ImageFont.load_default()
            self.font_button_temp = ImageFont.load_default()
            self.font_button_loc = ImageFont.load_default()

    def get_resized_icon(self, name, size_tuple):
        cache_key = (name, size_tuple)
        if cache_key in self.icon_cache:
            return self.icon_cache[cache_key]
        
        icon_path = self.plugin_base.get_icon_path(name)
        try:
            with Image.open(icon_path) as img:
                resized = img.convert("RGBA").resize(size_tuple, Image.Resampling.LANCZOS)
                self.icon_cache[cache_key] = resized
                return resized
        except Exception as e:
            log.error(f"Error loading icon {name}: {e}")
            return None

    def get_resized_background(self, name, size_tuple):
        cache_key = ("bg_" + name, size_tuple)
        if cache_key in self.icon_cache:
            return self.icon_cache[cache_key]
        
        filename_map = {
            "dawn": "Dawn.png",
            "day": "Day.png",
            "dusk": "dusk.png",
            "night": "night.png",
            "forecast": "forecast_background.png",
            "button_dawn": "button-dawn.png",
            "button_day": "button-day.png",
            "button_dusk": "button-dusk.png",
            "button_night": "button-night.png",
            "rain": "rain-background.png",
            "button_rain": "rain-background-button.png"
        }
        filename = filename_map.get(name, "Day.png")
        bg_path = os.path.join(self.plugin_base.PATH, "assets", "sky-cycles", filename)
        
        try:
            with Image.open(bg_path) as img:
                resized = img.convert("RGBA").resize(size_tuple, Image.Resampling.LANCZOS)
                self.icon_cache[cache_key] = resized
                return resized
        except Exception as e:
            log.error(f"Error loading background {name}: {e}")
            return None

    def get_font(self, font_path, size):
        if not font_path or not os.path.exists(font_path):
            font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        cache_key = ("font", font_path, size)
        if cache_key in self.icon_cache:
            return self.icon_cache[cache_key]
        try:
            font = ImageFont.truetype(font_path, size)
            self.icon_cache[cache_key] = font
            return font
        except Exception:
            try:
                font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", size)
                self.icon_cache[cache_key] = font
                return font
            except Exception:
                return ImageFont.load_default()

    def on_ready(self):
        self.show()

    def on_key_down(self):
        self.show(force=True)

    def event_callback(self, event: InputEvent, data: dict = None):
        if event == Input.Dial.Events.TURN_CW:
            self.display_page = (self.display_page + 1) % 3
            self.reset_page_timer()
            self.show()
        elif event == Input.Dial.Events.TURN_CCW:
            self.display_page = (self.display_page - 1) % 3
            self.reset_page_timer()
            self.show()
        elif event == Input.Dial.Events.SHORT_TOUCH_PRESS:
            self.display_page = (self.display_page + 1) % 3
            self.reset_page_timer()
            self.show()
        else:
            super().event_callback(event, data)
            
    def reset_page_timer(self):
        if self.page_timer is not None:
            self.page_timer.cancel()
        self.page_timer = Timer(5.0, self.revert_to_default_page)
        self.page_timer.start()

    def revert_to_default_page(self):
        if not self.get_is_present():
            return
        self.display_page = 0
        self.show()

    def get_config_rows(self) -> list:
        self.units_model = Gtk.ListStore.new([str, int])
        self.units_row = ComboRow(title=self.plugin_base.lm.get("actions.unit.title"), model=self.units_model)
        self.lat_entry = Adw.EntryRow(title=self.plugin_base.lm.get("actions.lat-entry.title"), input_purpose=Gtk.InputPurpose.NUMBER)
        self.lon_entry = Adw.EntryRow(title=self.plugin_base.lm.get("actions.long-entry.title"), input_purpose=Gtk.InputPurpose.NUMBER)
        self.loc_entry = Adw.EntryRow(title="Location Name")

        self.font_path_entry = Adw.EntryRow(title="Font Path (.ttf)")
        self.font_size_temp_entry = Adw.EntryRow(title="Temperature Font Size", input_purpose=Gtk.InputPurpose.NUMBER)
        self.font_size_loc_entry = Adw.EntryRow(title="Location Font Size", input_purpose=Gtk.InputPurpose.NUMBER)

        self.units_cell_renderer = Gtk.CellRendererText()
        self.units_row.combo_box.pack_start(self.units_cell_renderer, True)
        self.units_row.combo_box.add_attribute(self.units_cell_renderer, "text", 0)

        self.load_units_model()
        self.load_config_defaults()

        # Connect signals
        self.lat_entry.connect("notify::text", self.on_lat_changed)
        self.lon_entry.connect("notify::text", self.on_lon_changed)
        self.loc_entry.connect("notify::text", self.on_loc_changed)
        self.font_path_entry.connect("notify::text", self.on_font_path_changed)
        self.font_size_temp_entry.connect("notify::text", self.on_font_size_temp_changed)
        self.font_size_loc_entry.connect("notify::text", self.on_font_size_loc_changed)
        self.units_row.combo_box.connect("changed", self.on_units_changed)

        return [self.loc_entry, self.lat_entry, self.lon_entry, self.units_row, self.font_path_entry, self.font_size_temp_entry, self.font_size_loc_entry]
    
    def load_units_model(self):
        self.units_model.append([self.plugin_base.lm.get("actions.units.celsius"), 1])
        self.units_model.append([self.plugin_base.lm.get("actions.units.fahrenheit"), 2])
    
    def get_custom_config_area(self):
        return Gtk.Label(label=self.plugin_base.lm.get("actions.open-meteo-thanks"))
    
    def on_lat_changed(self, entry, *args):
        settings = self.get_settings()
        settings["lat"] = entry.get_text()
        self.set_settings(settings)
        self.show(force=True)
    
    def on_lon_changed(self, entry, *args):
        settings = self.get_settings()
        settings["lon"] = entry.get_text()
        self.set_settings(settings)
        self.show(force=True)

    def on_loc_changed(self, entry, *args):
        settings = self.get_settings()
        settings["location_name"] = entry.get_text()
        self.set_settings(settings)
        self.show(force=True)

    def on_units_changed(self, combo_box, *args):
        unit = self.units_model[combo_box.get_active()][1]
        settings = self.get_settings()
        settings["unit"] = unit
        self.set_settings(settings)
        self.show(force=True)

    def on_font_path_changed(self, entry, *args):
        settings = self.get_settings()
        settings["font_path"] = entry.get_text()
        self.set_settings(settings)
        self.show(force=True)

    def on_font_size_temp_changed(self, entry, *args):
        settings = self.get_settings()
        try:
            settings["font_size_temp"] = int(entry.get_text())
        except ValueError:
            settings["font_size_temp"] = 16
        self.set_settings(settings)
        self.show(force=True)

    def on_font_size_loc_changed(self, entry, *args):
        settings = self.get_settings()
        try:
            settings["font_size_loc"] = int(entry.get_text())
        except ValueError:
            settings["font_size_loc"] = 9
        self.set_settings(settings)
        self.show(force=True)

    def load_config_defaults(self):
        settings = self.get_settings()
        self.lat_entry.set_text(settings.get("lat", ""))  # Does not accept None
        self.lon_entry.set_text(settings.get("lon", ""))  # Does not accept None
        self.loc_entry.set_text(settings.get("location_name", "Washington DC"))
        self.font_path_entry.set_text(settings.get("font_path", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"))
        self.font_size_temp_entry.set_text(str(settings.get("font_size_temp", 16)))
        self.font_size_loc_entry.set_text(str(settings.get("font_size_loc", 9)))

        if settings.get("unit") == 2:  # Imperial
            self.units_row.combo_box.set_active(1)
        else:  # Celsius and none
            self.units_row.combo_box.set_active(0)

    def show(self, force=False):
        if not self.get_is_present():
            return
        # Stop timer if active
        if self.show_timer is not None:
            self.show_timer.cancel()

        weather = self.get_weather(force=force)
        if weather is None:
            self.show_error()
            return
        
        is_dial = isinstance(self.input_ident, Input.Dial)
        
        if is_dial:
            image = self.render_dial_image(weather)
            self.set_media(image=image, size=1.0, valign=0, halign=0)
            self.set_bottom_label("")
        else:
            image = self.render_button_image(weather)
            self.set_media(image=image, size=1.0, valign=0, halign=0)
            
            self.set_top_label("")
            self.set_center_label("")
            self.set_bottom_label("")

        # Launch timer
        self.show_timer = Timer(self.show_interval * 60, self.show)
        self.show_timer.start()

    def get_weather(self, force=False) -> dict:
        now_time = time.time()
        if not force and self.cached_weather is not None and self.last_fetch_time is not None:
            if now_time - self.last_fetch_time < self.show_interval * 60:
                return self.cached_weather

        settings = self.get_settings()
        lat = settings.get("lat")
        lon = settings.get("lon")
        imperial = settings.get("unit") == 2

        try:
            lat = float(lat)
            lon = float(lon)
        except (TypeError, ValueError):
            return None

        # Check global provider settings
        global_settings = self.plugin_base.get_settings()
        provider = global_settings.get("provider", "open-meteo")
        api_key = global_settings.get("api_key", "")

        if provider == "open-meteo" or not api_key:
            result = self.get_weather_open_meteo(lat, lon, imperial)
        elif provider == "openweathermap":
            result = self.get_weather_openweathermap(lat, lon, api_key, imperial)
        elif provider == "wunderground":
            result = self.get_weather_wunderground(lat, lon, api_key, imperial)
        elif provider == "weathercom":
            result = self.get_weather_weathercom(lat, lon, api_key, imperial)
        else:
            result = self.get_weather_open_meteo(lat, lon, imperial)

        if result:
            self.cached_weather = result
            self.last_fetch_time = now_time
        return result

    def get_weather_open_meteo(self, lat, lon, imperial) -> dict:
        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": lat,
            "longitude": lon,
            "current": ["weather_code", "is_day", "temperature_2m"],
            "daily": ["weather_code", "temperature_2m_max", "temperature_2m_min"],
            "hourly": ["temperature_2m"],
            "timezone": "auto"
        }
        if imperial:
            params["temperature_unit"] = "fahrenheit"
            
        try:
            resp = requests.get(url, params=params, timeout=5)
            if resp.status_code != 200:
                log.error(f"Open-Meteo failed with status {resp.status_code}")
                return None
            data = resp.json()
        except Exception as e:
            log.error(f"Open-Meteo request failed: {e}")
            return None
            
        # Parse current
        current_data = {
            "weather_code": data["current"]["weather_code"],
            "is_day": bool(data["current"]["is_day"]),
            "temperature": data["current"]["temperature_2m"],
            "temperature_unit": data["current_units"]["temperature_2m"]
        }
        
        # Parse daily forecast
        days = []
        for date_str in data["daily"]["time"]:
            try:
                dt = datetime.datetime.strptime(date_str, "%Y-%m-%d")
                days.append(dt.strftime("%a"))
            except Exception:
                days.append("")
                
        daily_data = {
            "days": days,
            "codes": data["daily"]["weather_code"],
            "max_temps": data["daily"]["temperature_2m_max"],
            "min_temps": data["daily"]["temperature_2m_min"]
        }
        
        # Parse hourly forecast
        current_iso = data["current"]["time"]
        hourly_times = data["hourly"]["time"]
        hourly_temps = data["hourly"]["temperature_2m"]
        
        start_idx = 0
        for idx, t_str in enumerate(hourly_times):
            if t_str >= current_iso:
                start_idx = idx
                break
                
        sampled_times = []
        sampled_temps = []
        for h in range(0, 24, 2):
            idx = start_idx + h
            if idx < len(hourly_times):
                t_str = hourly_times[idx]
                try:
                    dt = datetime.datetime.strptime(t_str, "%Y-%m-%dT%H:%M")
                    sampled_times.append(dt.strftime("%-I%p"))
                except Exception:
                    sampled_times.append("")
                sampled_temps.append(hourly_temps[idx])
                
        hourly_data = {
            "times": sampled_times,
            "temps": sampled_temps
        }
        
        return {
            "current": current_data,
            "daily": daily_data,
            "hourly": hourly_data
        }

    def get_weather_openweathermap(self, lat, lon, api_key, imperial) -> dict:
        url = "https://api.openweathermap.org/data/2.5/forecast"
        params = {
            "lat": lat,
            "lon": lon,
            "appid": api_key,
            "units": "imperial" if imperial else "metric"
        }
        try:
            resp = requests.get(url, params=params, timeout=5)
            if resp.status_code != 200:
                log.error(f"OWM failed with status {resp.status_code}")
                return self.get_weather_open_meteo(lat, lon, imperial)
            data = resp.json()
        except Exception as e:
            log.error(f"OWM request failed: {e}")
            return self.get_weather_open_meteo(lat, lon, imperial)
            
        forecast_list = data.get("list", [])
        if not forecast_list:
            return self.get_weather_open_meteo(lat, lon, imperial)
            
        first = forecast_list[0]
        weather_code = owm_to_wmo(first["weather"][0]["id"])
        icon_name = first["weather"][0]["icon"]
        is_day = icon_name.endswith("d")
        
        current_data = {
            "weather_code": weather_code,
            "is_day": is_day,
            "temperature": first["main"]["temp"],
            "temperature_unit": "°F" if imperial else "°C"
        }
        
        # Group list items by day (YYYY-MM-DD)
        days_dict = {}
        for item in forecast_list:
            dt_txt = item["dt_txt"]
            date_str = dt_txt.split(" ")[0]
            if date_str not in days_dict:
                days_dict[date_str] = []
            days_dict[date_str].append(item)
            
        days = []
        codes = []
        max_temps = []
        min_temps = []
        
        for date_str in sorted(days_dict.keys())[:5]:
            items = days_dict[date_str]
            temps = [x["main"]["temp"] for x in items]
            max_temps.append(max(temps))
            min_temps.append(min(temps))
            
            midday_item = items[len(items) // 2]
            for item in items:
                if "12:00:00" in item["dt_txt"]:
                    midday_item = item
                    break
            codes.append(owm_to_wmo(midday_item["weather"][0]["id"]))
            
            try:
                dt = datetime.datetime.strptime(date_str, "%Y-%m-%d")
                days.append(dt.strftime("%a"))
            except Exception:
                days.append("")
                
        daily_data = {
            "days": days,
            "codes": codes,
            "max_temps": max_temps,
            "min_temps": min_temps
        }
        
        sampled_times = []
        sampled_temps = []
        for item in forecast_list[:12]:
            dt_txt = item["dt_txt"]
            try:
                dt = datetime.datetime.strptime(dt_txt, "%Y-%m-%d %H:%M:%S")
                sampled_times.append(dt.strftime("%-I%p"))
            except Exception:
                sampled_times.append("")
            sampled_temps.append(item["main"]["temp"])
            
        hourly_data = {
            "times": sampled_times[::2],
            "temps": sampled_temps[::2]
        }
        
        return {
            "current": current_data,
            "daily": daily_data,
            "hourly": hourly_data
        }

    def get_weather_wunderground(self, lat, lon, api_key, imperial) -> dict:
        return self.get_weather_weathercom(lat, lon, api_key, imperial)

    def get_weather_weathercom(self, lat, lon, api_key, imperial) -> dict:
        units_param = "e" if imperial else "m"
        
        current_url = "https://api.weather.com/v3/wx/conditions/current"
        current_params = {
            "geocode": f"{lat},{lon}",
            "format": "json",
            "units": units_param,
            "apiKey": api_key,
            "language": "en-US"
        }
        
        daily_url = "https://api.weather.com/v3/wx/forecast/daily/5day"
        daily_params = {
            "geocode": f"{lat},{lon}",
            "format": "json",
            "units": units_param,
            "apiKey": api_key,
            "language": "en-US"
        }
        
        hourly_url = "https://api.weather.com/v3/wx/forecast/hourly/2day"
        hourly_params = {
            "geocode": f"{lat},{lon}",
            "format": "json",
            "units": units_param,
            "apiKey": api_key,
            "language": "en-US"
        }
        
        try:
            curr_resp = requests.get(current_url, params=current_params, timeout=5)
            daily_resp = requests.get(daily_url, params=daily_params, timeout=5)
            hour_resp = requests.get(hourly_url, params=hourly_params, timeout=5)
            
            if curr_resp.status_code != 200 or daily_resp.status_code != 200 or hour_resp.status_code != 200:
                return self.get_weather_open_meteo(lat, lon, imperial)
                
            curr_data = curr_resp.json()
            daily_data_raw = daily_resp.json()
            hourly_data_raw = hour_resp.json()
        except Exception as e:
            log.error(f"Weather.com API error: {e}")
            return self.get_weather_open_meteo(lat, lon, imperial)
            
        temp_unit = "°F" if imperial else "°C"
        current_data = {
            "weather_code": twc_to_wmo(curr_data.get("iconCode")),
            "is_day": curr_data.get("dayOrNight") == "D",
            "temperature": curr_data.get("temperature"),
            "temperature_unit": temp_unit
        }
        
        days = []
        codes = []
        max_temps = []
        min_temps = []
        
        day_names = daily_data_raw.get("dayOfWeek", [])
        icon_codes = daily_data_raw.get("calendarDayIconCode", [])
        t_max_list = daily_data_raw.get("temperatureMax", [])
        t_min_list = daily_data_raw.get("temperatureMin", [])
        
        for i in range(min(5, len(day_names))):
            days.append(day_names[i][:3])
            codes.append(twc_to_wmo(icon_codes[i] if i < len(icon_codes) else 0))
            max_temps.append(t_max_list[i] if i < len(t_max_list) else 0)
            min_temps.append(t_min_list[i] if i < len(t_min_list) else 0)
            
        daily_data = {
            "days": days,
            "codes": codes,
            "max_temps": max_temps,
            "min_temps": min_temps
        }
        
        times_raw = hourly_data_raw.get("validTimeLocal", [])
        temps_raw = hourly_data_raw.get("temperature", [])
        
        sampled_times = []
        sampled_temps = []
        for i in range(0, min(24, len(times_raw)), 2):
            t_str = times_raw[i]
            try:
                dt = datetime.datetime.fromisoformat(t_str)
                sampled_times.append(dt.strftime("%-I%p"))
            except Exception:
                sampled_times.append("")
            sampled_temps.append(temps_raw[i])
            
        hourly_data = {
            "times": sampled_times,
            "temps": sampled_temps
        }
        
        return {
            "current": current_data,
            "daily": daily_data,
            "hourly": hourly_data
        }

    def render_dial_image(self, weather_data) -> Image.Image:
        width, height = 200, 100
        canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(canvas)
        
        settings = self.get_settings()
        font_path = settings.get("font_path", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
        font_large = self.get_font(font_path, 28)
        font_medium = self.get_font(font_path, 12)
        font_title = self.get_font(font_path, 10)
        font_text = self.get_font(font_path, 8)

        current = weather_data.get("current", {})
        is_day = current.get("is_day", True)
        weather_code = current.get("weather_code", 0)
        
        now = datetime.datetime.now()
        hour = now.hour
        
        if self.display_page == 0:
            if is_rain_or_snow(weather_code):
                bg_name = "rain"
            else:
                if not is_day:
                    time_of_day = "night"
                elif 5 <= hour <= 7:
                    time_of_day = "dawn"
                elif 18 <= hour <= 20:
                    time_of_day = "dusk"
                else:
                    time_of_day = "day"
                bg_name = time_of_day
        else:
            bg_name = "forecast"
            
        bg = self.get_resized_background(bg_name, (width, height))
        if bg:
            canvas.paste(bg, (0, 0))
        else:
            draw.rectangle([0, 0, width, height], fill=(0, 0, 0, 255))
        
        if self.display_page == 0 and bg_name == "night":
            stars = [(20, 15), (60, 25), (140, 15), (180, 30), (90, 20)]
            for sx, sy in stars:
                draw.ellipse([sx - 1, sy - 1, sx + 1, sy + 1], fill=(255, 255, 255, 180))
                
        if self.display_page == 0:
            # Current Page
            weather_code = current.get("weather_code", 0)
            image_name = self.get_image_to_show(weather_code, not is_day)
            
            icon_img = self.get_resized_icon(image_name, (48, 48))
            if icon_img:
                canvas.paste(icon_img, (15, 20), icon_img)
                
            temp = current.get("temperature", 0)
            temp_unit = current.get("temperature_unit", "°C")
            action_settings = self.get_settings()
            location_name = action_settings.get("location_name", "Weather")
            
            temp_text = f"{int(temp)}{temp_unit}"
            draw.text((95, 15), temp_text, font=font_large, fill=(255, 255, 255, 255), stroke_width=2, stroke_fill=(0, 0, 0, 255))
            draw.text((95, 50), location_name, font=font_medium, fill=(255, 255, 255, 255), stroke_width=2, stroke_fill=(0, 0, 0, 255))
            
        elif self.display_page == 1:
            # 5-Day Page
            draw.text((100, 16), "5 Day Forecast", font=font_title, fill=(255, 255, 255, 255), anchor="mm", stroke_width=2, stroke_fill=(0, 0, 0, 255))
            
            daily = weather_data.get("daily", {})
            days = daily.get("days", [])[:5]
            codes = daily.get("codes", [])[:5]
            max_temps = daily.get("max_temps", [])[:5]
            
            col_width = 36
            start_x = 28
            for i in range(len(days)):
                cx = start_x + i * col_width
                
                day_label = f"{days[i]}." if days[i] else ""
                draw.text((cx, 30), day_label, font=font_text, fill=(255, 255, 255, 255), anchor="mm", stroke_width=2, stroke_fill=(0, 0, 0, 255))
                
                code = codes[i] if i < len(codes) else 0
                image_name = self.get_image_to_show(code, False)
                icon_img = self.get_resized_icon(image_name, (18, 18))
                if icon_img:
                    canvas.paste(icon_img, (cx - 9, 36), icon_img)
                    
                t_max = max_temps[i] if i < len(max_temps) else 0
                temp_text = f"{int(t_max)}°"
                draw.text((cx, 64), temp_text, font=font_medium, fill=(255, 255, 255, 255), anchor="mm", stroke_width=2, stroke_fill=(0, 0, 0, 255))
                
        elif self.display_page == 2:
            # Hourly Page
            draw.text((100, 16), "Hourly Forecast", font=font_title, fill=(255, 255, 255, 255), anchor="mm", stroke_width=2, stroke_fill=(0, 0, 0, 255))
            
            hourly = weather_data.get("hourly", {})
            times = hourly.get("times", [])[:24]
            temps = hourly.get("temps", [])[:24]
            
            if temps:
                min_t, max_t = min(temps), max(temps)
                t_range = (max_t - min_t) if max_t != min_t else 1.0
                
                graph_x_start = 32
                graph_x_end = 168
                graph_y_start = 65
                graph_y_end = 35
                
                points = []
                num_points = len(temps)
                dx = (graph_x_end - graph_x_start) / (num_points - 1)
                for i in range(num_points):
                    px = int(graph_x_start + i * dx)
                    py = int(graph_y_start - ((temps[i] - min_t) / t_range) * (graph_y_start - graph_y_end))
                    points.append((px, py))
                    
                draw.line(points, fill=(255, 215, 0, 255), width=2)
                
                draw.text((graph_x_start - 6, graph_y_start), f"{int(min_t)}°", font=font_text, fill=(255, 255, 255, 255), anchor="rm", stroke_width=2, stroke_fill=(0, 0, 0, 255))
                draw.text((graph_x_end + 6, graph_y_end), f"{int(max_t)}°", font=font_text, fill=(255, 255, 255, 255), anchor="lm", stroke_width=2, stroke_fill=(0, 0, 0, 255))
                
                x_labels = [0, 3, 6, 9, 11]
                for idx in x_labels:
                    if idx < len(times):
                        px = int(graph_x_start + idx * dx)
                        draw.text((px, 74), times[idx], font=font_text, fill=(255, 255, 255, 255), anchor="mm", stroke_width=2, stroke_fill=(0, 0, 0, 255))
                        
        dot_y = 92
        dot_spacing = 10
        dot_x_start = width / 2 - dot_spacing
        for d in range(3):
            dx = dot_x_start + d * dot_spacing
            if d == self.display_page:
                draw.ellipse([dx - 3, dot_y - 3, dx + 3, dot_y + 3], fill=(255, 255, 255, 255))
            else:
                draw.ellipse([dx - 2, dot_y - 2, dx + 2, dot_y + 2], fill=(255, 255, 255, 100))
                
        return canvas

    def render_button_image(self, weather_data) -> Image.Image:
        width, height = 113, 113
        canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        
        current = weather_data.get("current", {})
        is_day = current.get("is_day", True)
        weather_code = current.get("weather_code", 0)
        
        now = datetime.datetime.now()
        hour = now.hour
        
        if is_rain_or_snow(weather_code):
            bg_name = "button_rain"
        else:
            if not is_day:
                time_of_day = "night"
            elif 5 <= hour <= 7:
                time_of_day = "dawn"
            elif 18 <= hour <= 20:
                time_of_day = "dusk"
            else:
                time_of_day = "day"
            bg_name = "button_" + time_of_day
        bg = self.get_resized_background(bg_name, (width, height))
        if bg:
            canvas.paste(bg, (0, 0))
        else:
            draw = ImageDraw.Draw(canvas)
            draw.rectangle([0, 0, width, height], fill=(0, 0, 0, 255))
            
        # Paste weather icon in the center/upper part
        weather_code = current.get("weather_code", 0)
        image_name = self.get_image_to_show(weather_code, not is_day)
        icon_img = self.get_resized_icon(image_name, (44, 44))
        if icon_img:
            canvas.paste(icon_img, (34, 12), icon_img)
            
        # Draw temperature and location text
        temp = current.get("temperature", 0)
        temp_unit = current.get("temperature_unit", "°C")
        action_settings = self.get_settings()
        location_name = action_settings.get("location_name", "Weather")
        
        font_path = action_settings.get("font_path", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
        try:
            font_size_temp = int(action_settings.get("font_size_temp", 16))
        except ValueError:
            font_size_temp = 16
        try:
            font_size_loc = int(action_settings.get("font_size_loc", 9))
        except ValueError:
            font_size_loc = 9

        font_temp = self.get_font(font_path, font_size_temp)
        font_loc = self.get_font(font_path, font_size_loc)

        draw = ImageDraw.Draw(canvas)

        temp_text = f"{int(temp)}{temp_unit}"
        draw.text((width / 2, 68), temp_text, font=font_temp, fill=(255, 255, 255, 255), anchor="mm", stroke_width=2, stroke_fill=(0, 0, 0, 255))
        
        loc_display = location_name
        if len(loc_display) > 16:
            loc_display = loc_display[:14] + ".."
        draw.text((width / 2, 88), loc_display, font=font_loc, fill=(255, 255, 255, 255), anchor="mm", stroke_width=2, stroke_fill=(0, 0, 0, 255))
        
        return canvas

    def get_image_to_show(self, weather_code: int, night: bool) -> str:
        wc = weather_code
        if wc == 0:
            if night:
                return "clear_night"
            else:
                return "sunny"
        elif wc in range(1, 4):
            if night:
                return "cloudy_night"
            else:
                return "cloud"
        elif wc in range(45, 49):
            return "foggy"
        elif wc in range(51, 58):
            return "rainy_light"
        elif wc in range(61, 68) or wc in range(80, 87):
            return "rainy_heavy"
        elif wc in range(71, 78):
            return "snowy"
        elif wc in range(95, 100):
            return "thunderstorm"
        return "sunny"


class WeatherPlugin(PluginBase):
    def __init__(self):
        super().__init__()
        self.init_locale_manager()
        self.lm = self.locale_manager
        
        self.has_plugin_settings = True

        ## Register actions
        self.wind_direction_holder = ActionHolder(
            plugin_base=self,
            action_base=WindDirection,
            action_id_suffix="WindDirection",
            action_name=self.lm.get("actions.wind-direction.name"),
            icon=Gtk.Image(icon_name="weather-windy-symbolic"),
            action_support={
                Input.Key: ActionInputSupport.SUPPORTED,
                Input.Dial: ActionInputSupport.SUPPORTED,
                Input.Touchscreen: ActionInputSupport.UNSUPPORTED
            }
        )
        self.add_action_holder(self.wind_direction_holder)

        self.weather_holder = ActionHolder(
            plugin_base=self,
            action_base=Weather,
            action_id_suffix="Weather",
            action_name=self.lm.get("actions.weather.name"),
            icon=Gtk.Image(icon_name="weather-clear-symbolic"),
            action_support={
                Input.Key: ActionInputSupport.SUPPORTED,
                Input.Dial: ActionInputSupport.SUPPORTED,
                Input.Touchscreen: ActionInputSupport.UNSUPPORTED
            }
        )
        self.add_action_holder(self.weather_holder)

        # Register plugin
        self.register(
            plugin_name=self.lm.get("plugin.name"),
            github_repo="https://github.com/StreamController/Weather",
            plugin_version="1.0.0",
            app_version="1.0.0-alpha"
        )

    def init_locale_manager(self):
        self.lm = self.locale_manager
        self.lm.set_to_os_default()

    def get_selector_icon(self) -> Gtk.Widget:
        return Gtk.Image(icon_name="weather-clear-symbolic")

    def get_icon_path(self, image_name: str) -> str:
        settings = self.get_settings()
        selected_pack_name = settings.get("icon_pack", "default")
        
        if selected_pack_name != "default":
            try:
                packs = gl.icon_pack_manager.get_icon_packs()
                pack = packs.get(selected_pack_name)
                if pack:
                    for icon in pack.get_icons():
                        if icon.name == image_name:
                            return icon.path
            except Exception as e:
                log.error(f"Error loading icon pack icon {image_name}: {e}")
                
        # Default fallback
        return os.path.join(self.PATH, "assets", "weather-icons", f"{image_name}.png")

    def get_settings_area(self):
        group = Adw.PreferencesGroup(title="Global Weather Settings")
        
        # 1. Weather Provider setting
        provider_model = Gtk.ListStore.new([str, str])
        provider_model.append(["Open-Meteo", "open-meteo"])
        provider_model.append(["OpenWeatherMap", "openweathermap"])
        provider_model.append(["Weather Underground", "wunderground"])
        provider_model.append(["Weather.com", "weathercom"])
        
        provider_row = ComboRow(title="Weather Provider", model=provider_model)
        cell = Gtk.CellRendererText()
        provider_row.combo_box.pack_start(cell, True)
        provider_row.combo_box.add_attribute(cell, "text", 0)
        
        # 2. API Key setting
        key_row = Adw.EntryRow(title="API Key")
        
        # 3. Icon Pack setting
        icon_pack_model = Gtk.ListStore.new([str, str])
        icon_pack_model.append(["Plugin Default", "default"])
        
        try:
            packs = gl.icon_pack_manager.get_icon_packs()
            for name, pack in packs.items():
                icon_pack_model.append([pack.name, name])
        except Exception as e:
            log.error(f"Error loading icon packs: {e}")
            
        icon_pack_row = ComboRow(title="Icon Pack", model=icon_pack_model)
        cell2 = Gtk.CellRendererText()
        icon_pack_row.combo_box.pack_start(cell2, True)
        icon_pack_row.combo_box.add_attribute(cell2, "text", 0)
        
        # Load current settings values
        settings = self.get_settings()
        provider = settings.get("provider", "open-meteo")
        api_key = settings.get("api_key", "")
        selected_icon_pack = settings.get("icon_pack", "default")
        
        # Set active provider
        active_idx = 0
        for i, row in enumerate(provider_model):
            if row[1] == provider:
                active_idx = i
                break
        provider_row.combo_box.set_active(active_idx)
        
        # Set API Key
        key_row.set_text(api_key)
        
        # Set active icon pack
        active_pack_idx = 0
        for i, row in enumerate(icon_pack_model):
            if row[1] == selected_icon_pack:
                active_pack_idx = i
                break
        icon_pack_row.combo_box.set_active(active_pack_idx)
        
        # Define signal handlers to save changes
        def on_provider_changed(combo, *args):
            active = combo.get_active()
            if active >= 0:
                prov = provider_model[active][1]
                s = self.get_settings()
                s["provider"] = prov
                self.set_settings(s)
                
        def on_api_key_changed(entry, *args):
            s = self.get_settings()
            s["api_key"] = entry.get_text()
            self.set_settings(s)
            
        def on_icon_pack_changed(combo, *args):
            active = combo.get_active()
            if active >= 0:
                pack_name = icon_pack_model[active][1]
                s = self.get_settings()
                s["icon_pack"] = pack_name
                self.set_settings(s)
                
        provider_row.combo_box.connect("changed", on_provider_changed)
        key_row.connect("notify::text", on_api_key_changed)
        icon_pack_row.combo_box.connect("changed", on_icon_pack_changed)
        
        group.add(provider_row)
        group.add(key_row)
        group.add(icon_pack_row)
        
        return group