from django.urls import path

from . import places

urlpatterns = [
    path("drive/", places.drive, name="drive"),
    path("places/", places.places_page, name="places"),
    path("places/<int:pk>/delete/", places.place_delete, name="place_delete"),
    path("places/export.<str:fmt>", places.places_export, name="places_export"),
    path("places/import/", places.places_import, name="places_import"),
    path("places/geocode/", places.geocode, name="places_geocode"),
]
