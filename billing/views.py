from django.shortcuts import render
from djstripe.models import Product
from . import metadata

def pricing_page(request):
    products = Product.objects.filter(active=True).prefetch_related("prices")

    for product in products:
        meta = metadata.PRODUCTS.get(product.id)
        product.features_list = meta.features if meta else []

    return render(
        request, "billing/pricing_page.html", {"products": products}
    )
