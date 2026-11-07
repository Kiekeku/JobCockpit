# Local ATS Mock Site

The mock site runs on localhost and provides provider-aware ATS listing/detail endpoints plus application form pages for field detection, native file inputs, drop zones, and submit guard testing.

Routes use `/ats/{provider}/{tenant}/...` so additional adapters such as Oracle-style or other ATS surfaces can be added without changing the rest of the app.

