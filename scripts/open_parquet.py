# impport parquet
import pyarrow.parquet as pq
table = pq.read_table(r"C:\Users\Ali\OneDrive - CUNY\Desktop\SI\fimbox_SI26\data\usgs_rating_curves.parquet")
print(table)