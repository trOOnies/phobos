# Bundled data

`places.tsv.gz` and `countries.tsv` let Phobos name the place a photo was taken
(city, region, country) from its GPS coordinates without going online.

They are derived from [GeoNames](https://www.geonames.org) (`cities5000`,
`admin1CodesASCII`, `countryInfo`), licensed under
[Creative Commons Attribution 4.0](https://creativecommons.org/licenses/by/4.0/).
Rebuild them with `scripts/build_places.py`.
