from django.shortcuts import render
from djstripe.models import Product
from . import metadata
from django.conf import settings
from django.contrib.auth.decorators import login_required
from djstripe.settings import djstripe_settings


@login_required
def pricing_page(request):
    products = Product.objects.filter(active=True).prefetch_related("prices")

    for product in products:
        meta = metadata.PRODUCTS.get(product.id)
        product.features_list = meta.features if meta else []

    return render(
        request, "billing/pricing_page.html",
        context={"stripe_public_key": settings.STRIPE_PRICING_TABLE_KEY,
                "stripe_pricing_table_id": settings.STRIPE_PRICING_TABLE_ID,
                "products": products},
    )
