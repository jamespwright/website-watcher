// Run in the page context of any https://www.tesla.com/en_AU/... tab (Claude-in-Chrome's
// javascript_tool does this). Returns the JSON to save as data/tesla-snapshot.json.
// Tesla's Akamai wall blocks scripts but not the page's own same-origin fetch in a real browser.
// Edit MODEL / TRIM / ZIP to match the profile (`search.postcode`, `source_options.tesla.TRIM`).
(async () => {
  const MODEL = "my", TRIM = ["MY_LR_AWD"], ZIP = "3056";
  const q = {
    query: {
      model: MODEL, condition: "used", options: { TRIM }, arrangeby: "Price", order: "asc",
      market: "AU", language: "en", super_region: "north america", PaymentType: "cash",
      paymentRange: {}, zip: ZIP, range: 0,
    },
    offset: 0, count: 50, outsideOffset: 0, outsideSearch: false,
  };
  const res = await fetch("/inventory/api/v4/inventory-results?query=" + encodeURIComponent(JSON.stringify(q)),
                          { credentials: "include" });
  const j = await res.json();
  // Note: the API rejects a RegistrationProvince option (412). Prices come back per state instead.
  return JSON.stringify({
    captured_at: new Date().toISOString(),
    model: MODEL,
    vehicles: j.results.map(r => ({
      vin: r.VIN, trim: r.TrimName, year: +String(r.Year).slice(0, 4), odometer: r.Odometer,
      paint: (r.PAINT || []).join("/"), city: r.City, state: r.StateProvince,
      in_transit: r.InTransit, first_registered: String(r.FirstRegistrationDate).slice(0, 10),
      prices: Object.fromEntries(Object.entries(r.CashDetailsByRegion || {}).map(([k, v]) => [k, v.cash.grossPrice])),
    })),
  });
})()
