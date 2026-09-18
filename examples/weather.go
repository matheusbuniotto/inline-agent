package weather

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"time"
)

// CurrentWeather represents the current weather conditions.
type CurrentWeather struct {
	Time          string  `json:"time"`
	Interval      int     `json:"interval"`
	Temperature   float64 `json:"temperature"`
	WindSpeed     float64 `json:"windspeed"`
	WindDirection float64 `json:"winddirection"`
	IsDay         int     `json:"is_day"`
	WeatherCode   int     `json:"weathercode"`
}

// CurrentWeatherUnits defines the units for the current weather measurements.
type CurrentWeatherUnits struct {
	Time          string `json:"time"`
	Interval      string `json:"interval"`
	Temperature   string `json:"temperature"`
	WindSpeed     string `json:"windspeed"`
	WindDirection string `json:"winddirection"`
	IsDay         string `json:"is_day"`
	WeatherCode   string `json:"weathercode"`
}

// WeatherData represents the response returned by the Open-Meteo API.
type WeatherData struct {
	Latitude             float64             `json:"latitude"`
	Longitude            float64             `json:"longitude"`
	GenerationTimeMs     float64             `json:"generationtime_ms"`
	UtcOffsetSeconds     int                 `json:"utc_offset_seconds"`
	Timezone             string              `json:"timezone"`
	TimezoneAbbreviation string              `json:"timezone_abbreviation"`
	Elevation            float64             `json:"elevation"`
	CurrentWeatherUnits  CurrentWeatherUnits `json:"current_weather_units"`
	CurrentWeather       CurrentWeather      `json:"current_weather"`
}

// FetchWeather queries the Open-Meteo API with a 3s context timeout.
// Returns WeatherData or wrapped error.
func FetchWeather(latitude, longitude float64) (*WeatherData, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	return FetchWeatherWithContext(ctx, latitude, longitude)
}

// FetchWeatherWithContext queries the Open-Meteo API with the provided context, bounded by a 3s timeout.
// Returns WeatherData or wrapped error.
func FetchWeatherWithContext(ctx context.Context, latitude, longitude float64) (*WeatherData, error) {
	if ctx == nil {
		ctx = context.Background()
	}
	ctx, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()

	endpoint := fmt.Sprintf("https://api.open-meteo.com/v1/forecast?latitude=%f&longitude=%f&current_weather=true", latitude, longitude)

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("failed to fetch weather data: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("open-meteo api returned status %d: %s", resp.StatusCode, resp.Status)
	}

	var data WeatherData
	if err := json.NewDecoder(resp.Body).Decode(&data); err != nil {
		return nil, fmt.Errorf("failed to decode weather response: %w", err)
	}

	return &data, nil
}
