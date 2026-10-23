"""Melbourne's Cheapest Cars (Oakleigh VIC) - a DealerStudio site."""
from watcher.core.source import register_source

from .dealerstudio import DealerStudioSource


@register_source
class MelbournesCheapestCars(DealerStudioSource):
    name = "melbournescheapestcars"
    label = "Melbourne's Cheapest Cars"
    base_url = "https://www.melbournescheapestcars.com.au"
    notes = ("DealerStudio platform: public Meilisearch API (scoped key discovered from site JS), "
             "fallback to Next.js RSC page data.")
