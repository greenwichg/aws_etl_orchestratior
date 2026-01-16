# AWS Glue + PySpark Dimensional Data Warehouse Implementation
## Complete Guide: SCD Type 2, Incremental Loads, and MERGE Operations

---

## Table of Contents

1. [Glue + Spark Architecture Overview](#architecture)
2. [SCD Type 2 Implementation in PySpark](#scd-type2-pyspark)
3. [Incremental Loading Patterns](#incremental-loading)
4. [MERGE/UPSERT to Redshift](#merge-upsert)
5. [Partitioning Strategy](#partitioning)
6. [Performance Optimization](#optimization)
7. [Complete Glue Job Examples](#complete-jobs)
8. [Error Handling and Logging](#error-handling)
9. [Job Orchestration with Step Functions](#orchestration)

---

## 1. Glue + Spark Architecture Overview {#architecture}

### Your Current ETL Flow

```
S3 (Raw CSVs)
    ↓
EventBridge (File Arrival)
    ↓
Lambda (Validation)
    ↓
Step Functions (Orchestration)
    ↓
AWS Glue Job (PySpark) ← We focus here
    ↓
S3 (Staging/Parquet)
    ↓
Redshift COPY
    ↓
Redshift (Final Tables)
```

### Recommended Architecture for Dimensional Model

```
┌─────────────────────────────────────────────────────────┐
│ S3: Raw Layer (CSV from Anaplan)                        │
│ s3://bucket/raw/aum_revenue/2025/01/15/file.csv        │
└─────────────────────────────────────────────────────────┘
                    ↓
┌─────────────────────────────────────────────────────────┐
│ Glue Job 1: Dimension Processing (SCD Type 2)          │
│ - Read from S3 raw                                      │
│ - Apply transformations                                 │
│ - Implement SCD Type 2 logic                           │
│ - Write to S3 staging (Parquet)                        │
└─────────────────────────────────────────────────────────┘
                    ↓
┌─────────────────────────────────────────────────────────┐
│ S3: Staging/Processed Layer (Parquet, partitioned)     │
│ s3://bucket/staging/dim_fund/year=2025/month=01/       │
└─────────────────────────────────────────────────────────┘
                    ↓
┌─────────────────────────────────────────────────────────┐
│ Glue Job 2: Fact Processing (Incremental)              │
│ - Read dimensions from staging                          │
│ - Enrich facts with dimension keys                     │
│ - Apply business rules                                 │
│ - Write to S3 staging                                  │
└─────────────────────────────────────────────────────────┘
                    ↓
┌─────────────────────────────────────────────────────────┐
│ Glue Job 3: Redshift Load (MERGE/COPY)                 │
│ - COPY from S3 to staging tables                        │
│ - Execute MERGE for UPSERT                             │
│ - Vacuum and analyze                                   │
└─────────────────────────────────────────────────────────┘
                    ↓
┌─────────────────────────────────────────────────────────┐
│ Redshift: Final Dimensional Model                      │
│ - Dim_Fund, Dim_Cost_Center, etc.                     │
│ - Fact_AUM_Revenue, Fact_Expense, etc.                │
└─────────────────────────────────────────────────────────┘
```

---

## 2. SCD Type 2 Implementation in PySpark {#scd-type2-pyspark}

### Pattern 1: Full SCD Type 2 in Spark (Before Redshift)

This approach does ALL SCD logic in Glue, then just loads final results to Redshift.

#### Glue Job: Process Dim_Fund with SCD Type 2

```python
import sys
from awsglue.transforms import *
from awsglue.utils import getResolvedOptions
from pyspark.context import SparkContext
from awsglue.context import GlueContext
from awsglue.job import Job
from pyspark.sql.functions import *
from pyspark.sql.window import Window
from datetime import datetime, timedelta

# Initialize
args = getResolvedOptions(sys.argv, ['JOB_NAME'])
sc = SparkContext()
glueContext = GlueContext(sc)
spark = glueContext.spark_session
job = Job(glueContext)
job.init(args['JOB_NAME'], args)

# Configuration
S3_RAW_PATH = "s3://nyl-invgai-dev-s3-anaplan-bucket/Data/Input/"
S3_STAGING_PATH = "s3://nyl-invgai-dev-s3-anaplan-bucket/Data/Staging/"
CURRENT_DATE = datetime.now().date()
EFFECTIVE_DATE = CURRENT_DATE
END_DATE_MAX = datetime(9999, 12, 31).date()

# ============================================================================
# STEP 1: Read Source Data (New/Changed Records)
# ============================================================================

source_df = spark.read \
    .option("header", "true") \
    .option("inferSchema", "true") \
    .csv(f"{S3_RAW_PATH}fund_master.csv")

# Clean column names
for old_col in source_df.columns:
    new_col = old_col.lower().replace(" ", "_").replace("-", "_")
    source_df = source_df.withColumnRenamed(old_col, new_col)

# Add source metadata
source_df = source_df \
    .withColumn("source_load_date", lit(CURRENT_DATE)) \
    .withColumn("source_system", lit("ANAPLAN"))

print(f"Source records: {source_df.count()}")

# ============================================================================
# STEP 2: Read Current Dimension (Existing Records)
# ============================================================================

try:
    # Read from S3 staging (Parquet) - previous dimension state
    current_dim_df = spark.read \
        .parquet(f"{S3_STAGING_PATH}dim_fund/")
    
    print(f"Current dimension records: {current_dim_df.count()}")
    
except Exception as e:
    print(f"No existing dimension found, creating new: {e}")
    # First load - create empty dataframe with schema
    current_dim_df = spark.createDataFrame([], schema=source_df.schema) \
        .withColumn("fund_key", lit(0).cast("int")) \
        .withColumn("effective_date", lit(CURRENT_DATE).cast("date")) \
        .withColumn("end_date", lit(None).cast("date")) \
        .withColumn("is_current", lit(True).cast("boolean")) \
        .withColumn("version", lit(0).cast("int"))

# ============================================================================
# STEP 3: Identify Changes (SCD Type 2 Logic)
# ============================================================================

# Get only current records
current_active_df = current_dim_df.filter(col("is_current") == True)

# Join source with current to detect changes
# Natural key: fund_code + share_class
comparison_df = source_df.alias("src") \
    .join(
        current_active_df.alias("curr"),
        (col("src.fund_code") == col("curr.fund_code")) &
        (col("src.share_class") == col("curr.share_class")),
        "left"
    )

# Identify change types
changes_df = comparison_df \
    .withColumn(
        "change_type",
        when(col("curr.fund_key").isNull(), "INSERT")  # New record
        .when(
            # Changed attributes (SCD Type 2 attributes)
            (col("src.management_fee") != col("curr.management_fee")) |
            (col("src.fund_name") != col("curr.fund_name")) |
            (col("src.fund_family_name") != col("curr.fund_family_name")),
            "UPDATE"
        )
        .otherwise("NO_CHANGE")
    )

# Filter to only changes
inserts_df = changes_df.filter(col("change_type") == "INSERT")
updates_df = changes_df.filter(col("change_type") == "UPDATE")

print(f"New records (INSERT): {inserts_df.count()}")
print(f"Changed records (UPDATE): {updates_df.count()}")

# ============================================================================
# STEP 4: Generate New Dimension Keys
# ============================================================================

# Get max existing key
if current_dim_df.count() > 0:
    max_key = current_dim_df.agg({"fund_key": "max"}).collect()[0][0]
else:
    max_key = 0

print(f"Max existing fund_key: {max_key}")

# Generate new keys for INSERTs
window_spec = Window.orderBy(monotonically_increasing_id())
inserts_with_keys_df = inserts_df \
    .withColumn("fund_key", row_number().over(window_spec) + max_key) \
    .withColumn("effective_date", lit(EFFECTIVE_DATE).cast("date")) \
    .withColumn("end_date", lit(None).cast("date")) \
    .withColumn("is_current", lit(True)) \
    .withColumn("version", lit(1))

# Generate new keys for UPDATEs (new version of existing record)
max_key_after_inserts = max_key + inserts_df.count()
updates_with_keys_df = updates_df \
    .withColumn("fund_key", row_number().over(window_spec) + max_key_after_inserts) \
    .withColumn("effective_date", lit(EFFECTIVE_DATE).cast("date")) \
    .withColumn("end_date", lit(None).cast("date")) \
    .withColumn("is_current", lit(True)) \
    .withColumn("version", col("curr.version") + 1)

# ============================================================================
# STEP 5: Close Old Records (for UPDATEs)
# ============================================================================

# Get list of fund_codes + share_class that changed
changed_natural_keys = updates_df.select(
    col("src.fund_code").alias("fund_code"),
    col("src.share_class").alias("share_class")
).distinct()

# Mark old records as expired
expired_records_df = current_active_df \
    .join(
        changed_natural_keys,
        (current_active_df.fund_code == changed_natural_keys.fund_code) &
        (current_active_df.share_class == changed_natural_keys.share_class),
        "inner"
    ) \
    .select(current_active_df["*"]) \
    .withColumn("end_date", lit(EFFECTIVE_DATE - timedelta(days=1)).cast("date")) \
    .withColumn("is_current", lit(False))

print(f"Records to expire: {expired_records_df.count()}")

# ============================================================================
# STEP 6: Combine All Records
# ============================================================================

# Select only source columns for new/changed records
source_columns = [
    "fund_key", "fund_code", "fund_name", "fund_family_code", 
    "fund_family_name", "share_class", "share_class_code",
    "management_fee", "asset_class", "strategy",
    "effective_date", "end_date", "is_current", "version"
]

# New records
new_records_df = inserts_with_keys_df.select(
    [col(f"src.{c}") if c in inserts_with_keys_df.columns 
     else col(c) for c in source_columns]
)

# Updated records (new versions)
updated_records_df = updates_with_keys_df.select(
    [col(f"src.{c}") if c in updates_with_keys_df.columns 
     else col(c) for c in source_columns]
)

# Unchanged current records
unchanged_current_df = current_active_df \
    .join(
        changed_natural_keys,
        (current_active_df.fund_code == changed_natural_keys.fund_code) &
        (current_active_df.share_class == changed_natural_keys.share_class),
        "left_anti"  # Keep records NOT in changed list
    ) \
    .select(source_columns)

# Expired records
expired_records_final_df = expired_records_df.select(source_columns)

# Union all
final_dimension_df = new_records_df \
    .union(updated_records_df) \
    .union(unchanged_current_df) \
    .union(expired_records_final_df)

# Add audit columns
final_dimension_df = final_dimension_df \
    .withColumn("created_date", current_timestamp()) \
    .withColumn("updated_date", current_timestamp()) \
    .withColumn("created_by", lit("glue_etl"))

print(f"Final dimension records: {final_dimension_df.count()}")

# ============================================================================
# STEP 7: Write to S3 Staging (Overwrite)
# ============================================================================

final_dimension_df.write \
    .mode("overwrite") \
    .partitionBy("is_current") \
    .parquet(f"{S3_STAGING_PATH}dim_fund/")

print("Dimension written to S3 staging successfully")

# ============================================================================
# STEP 8: Load to Redshift (Optional - can be separate job)
# ============================================================================

# This would typically be in a separate Glue job
# See "MERGE/UPSERT to Redshift" section below

job.commit()
```

---

### Pattern 2: Simplified SCD Type 2 (Staging + Redshift MERGE)

Process in Glue, then use Redshift MERGE for final SCD logic.

```python
# In Glue: Just prepare and stage the data
from pyspark.sql.functions import *

# Read source
source_df = spark.read.csv(f"{S3_RAW_PATH}fund_master.csv", header=True)

# Clean and transform
clean_df = source_df \
    .withColumn("load_timestamp", current_timestamp()) \
    .withColumn("source_system", lit("ANAPLAN"))

# Write to staging
clean_df.write \
    .mode("overwrite") \
    .parquet(f"{S3_STAGING_PATH}stg_fund/")

# Then in Redshift (via Glue JDBC or separate script):
# MERGE INTO dim_fund USING stg_fund ... (See Redshift MERGE section)
```

---

## 3. Incremental Loading Patterns {#incremental-loading}

### Pattern 1: Delta Load by Date

Load only yesterday's data:

```python
from pyspark.sql.functions import *
from datetime import datetime, timedelta

# Configuration
LOAD_DATE = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
S3_RAW_PATH = f"s3://bucket/raw/aum_revenue/date={LOAD_DATE}/"

# Read yesterday's data
source_df = spark.read \
    .option("header", "true") \
    .csv(S3_RAW_PATH)

# Process and load
# ... transformation logic ...

# Write with partition
output_df.write \
    .mode("append") \
    .partitionBy("year", "month", "day") \
    .parquet(f"{S3_STAGING_PATH}fact_aum_revenue/")
```

---

### Pattern 2: Incremental with Watermark Tracking

Track last processed timestamp:

```python
from pyspark.sql.functions import *
import boto3

# DynamoDB for watermark storage
dynamodb = boto3.resource('dynamodb')
watermark_table = dynamodb.Table('etl_watermarks')

# Get last watermark
response = watermark_table.get_item(Key={'job_name': 'aum_revenue_load'})
last_watermark = response.get('Item', {}).get('last_timestamp', '1900-01-01 00:00:00')

print(f"Last watermark: {last_watermark}")

# Read only new records
source_df = spark.read \
    .option("header", "true") \
    .csv(f"{S3_RAW_PATH}/*.csv")

# Filter by watermark
incremental_df = source_df.filter(col("last_modified") > last_watermark)

print(f"Incremental records: {incremental_df.count()}")

# Get new watermark
new_watermark = incremental_df.agg({"last_modified": "max"}).collect()[0][0]

# Process data
# ... transformation logic ...

# Update watermark after successful load
watermark_table.put_item(
    Item={
        'job_name': 'aum_revenue_load',
        'last_timestamp': str(new_watermark),
        'load_date': datetime.now().isoformat(),
        'records_processed': incremental_df.count()
    }
)

print(f"Updated watermark to: {new_watermark}")
```

---

### Pattern 3: CDC-Style Incremental (with Change Flags)

If source provides change indicators:

```python
from pyspark.sql.functions import *

# Read source with change type
source_df = spark.read \
    .option("header", "true") \
    .csv(f"{S3_RAW_PATH}/*.csv")

# Separate by operation type
inserts_df = source_df.filter(col("operation") == "I")
updates_df = source_df.filter(col("operation") == "U")
deletes_df = source_df.filter(col("operation") == "D")

# Process inserts
if inserts_df.count() > 0:
    inserts_df.write.mode("append").parquet(f"{S3_STAGING_PATH}fact/")

# Process updates (overwrite by key)
if updates_df.count() > 0:
    # Read existing
    existing_df = spark.read.parquet(f"{S3_STAGING_PATH}fact/")
    
    # Remove old versions
    updated_df = existing_df.join(
        updates_df.select("data_index", "time2"),
        ["data_index", "time2"],
        "left_anti"  # Keep records NOT being updated
    )
    
    # Add new versions
    final_df = updated_df.union(updates_df.drop("operation"))
    
    # Overwrite
    final_df.write.mode("overwrite").parquet(f"{S3_STAGING_PATH}fact/")

# Process deletes
if deletes_df.count() > 0:
    existing_df = spark.read.parquet(f"{S3_STAGING_PATH}fact/")
    
    # Remove deleted records
    final_df = existing_df.join(
        deletes_df.select("data_index", "time2"),
        ["data_index", "time2"],
        "left_anti"
    )
    
    final_df.write.mode("overwrite").parquet(f"{S3_STAGING_PATH}fact/")
```

---

## 4. MERGE/UPSERT to Redshift {#merge-upsert}

### Pattern 1: COPY to Staging + SQL MERGE (Recommended)

This is the most efficient approach for Redshift.

```python
import sys
from awsglue.transforms import *
from awsglue.utils import getResolvedOptions
from pyspark.context import SparkContext
from awsglue.context import GlueContext
from awsglue.job import Job
import boto3

# Initialize
args = getResolvedOptions(sys.argv, ['JOB_NAME'])
sc = SparkContext()
glueContext = GlueContext(sc)
spark = glueContext.spark_session
job = Job(glueContext)
job.init(args['JOB_NAME'], args)

# ============================================================================
# Configuration
# ============================================================================

S3_STAGING_PATH = "s3://nyl-invgai-dev-s3-anaplan-bucket/Data/Staging/dim_fund/"
REDSHIFT_WORKGROUP = "nyl-invgai-wg"
REDSHIFT_DATABASE = "nyl_anaplan_db"
SECRET_ARN = "arn:aws:secretsmanager:us-east-1:231139216201:secret:redshift/anaplan_batch_user-nWKC1N"
REGION = "us-east-1"

redshift_data_client = boto3.client('redshift-data', region_name=REGION)

# ============================================================================
# STEP 1: COPY from S3 to Redshift Staging Table
# ============================================================================

# Truncate staging table
truncate_sql = "TRUNCATE TABLE stg_dim_fund;"

response = redshift_data_client.execute_statement(
    WorkgroupName=REDSHIFT_WORKGROUP,
    Database=REDSHIFT_DATABASE,
    Sql=truncate_sql,
    SecretArn=SECRET_ARN
)

print(f"Truncate staging: {response['Id']}")

# Wait for truncate to complete
import time
while True:
    status = redshift_data_client.describe_statement(Id=response['Id'])
    if status['Status'] in ['FINISHED', 'FAILED', 'ABORTED']:
        print(f"Truncate status: {status['Status']}")
        break
    time.sleep(1)

# COPY command
copy_sql = f"""
COPY stg_dim_fund
FROM '{S3_STAGING_PATH}'
IAM_ROLE 'arn:aws:iam::231139216201:role/application/nyl-invgai-dev-redshift_s3_role'
FORMAT AS PARQUET;
"""

response = redshift_data_client.execute_statement(
    WorkgroupName=REDSHIFT_WORKGROUP,
    Database=REDSHIFT_DATABASE,
    Sql=copy_sql,
    SecretArn=SECRET_ARN
)

print(f"COPY command: {response['Id']}")

# Wait for COPY to complete
while True:
    status = redshift_data_client.describe_statement(Id=response['Id'])
    if status['Status'] in ['FINISHED', 'FAILED', 'ABORTED']:
        print(f"COPY status: {status['Status']}")
        if status['Status'] == 'FAILED':
            print(f"Error: {status.get('Error', 'Unknown error')}")
            raise Exception("COPY failed")
        break
    time.sleep(2)

# ============================================================================
# STEP 2: Execute MERGE for UPSERT
# ============================================================================

merge_sql = """
BEGIN TRANSACTION;

-- Close expired records
UPDATE dim_fund
SET 
    end_date = CURRENT_DATE - 1,
    is_current = FALSE,
    updated_date = GETDATE()
FROM stg_dim_fund s
WHERE dim_fund.fund_code = s.fund_code
  AND dim_fund.share_class = s.share_class
  AND dim_fund.is_current = TRUE
  AND (
      dim_fund.management_fee != s.management_fee OR
      dim_fund.fund_name != s.fund_name OR
      dim_fund.fund_family_name != s.fund_family_name
  );

-- Insert new versions (changes)
INSERT INTO dim_fund (
    fund_key, fund_code, fund_name, fund_family_code, fund_family_name,
    share_class, share_class_code, management_fee, asset_class, strategy,
    effective_date, end_date, is_current, version, created_date
)
SELECT 
    (SELECT COALESCE(MAX(fund_key), 0) + ROW_NUMBER() OVER (ORDER BY s.fund_code) 
     FROM dim_fund) as fund_key,
    s.fund_code,
    s.fund_name,
    s.fund_family_code,
    s.fund_family_name,
    s.share_class,
    s.share_class_code,
    s.management_fee,
    s.asset_class,
    s.strategy,
    CURRENT_DATE as effective_date,
    NULL as end_date,
    TRUE as is_current,
    COALESCE(d.version, 0) + 1 as version,
    GETDATE() as created_date
FROM stg_dim_fund s
LEFT JOIN dim_fund d 
    ON s.fund_code = d.fund_code 
    AND s.share_class = d.share_class
    AND d.end_date = CURRENT_DATE - 1  -- Just closed
WHERE d.fund_key IS NOT NULL  -- Changed records
   OR NOT EXISTS (  -- New records
       SELECT 1 FROM dim_fund d2
       WHERE d2.fund_code = s.fund_code
         AND d2.share_class = s.share_class
   );

END TRANSACTION;
"""

response = redshift_data_client.execute_statement(
    WorkgroupName=REDSHIFT_WORKGROUP,
    Database=REDSHIFT_DATABASE,
    Sql=merge_sql,
    SecretArn=SECRET_ARN
)

print(f"MERGE command: {response['Id']}")

# Wait for MERGE to complete
while True:
    status = redshift_data_client.describe_statement(Id=response['Id'])
    if status['Status'] in ['FINISHED', 'FAILED', 'ABORTED']:
        print(f"MERGE status: {status['Status']}")
        if status['Status'] == 'FAILED':
            print(f"Error: {status.get('Error', 'Unknown error')}")
            raise Exception("MERGE failed")
        break
    time.sleep(2)

# ============================================================================
# STEP 3: Vacuum and Analyze
# ============================================================================

vacuum_sql = """
VACUUM dim_fund;
ANALYZE dim_fund;
"""

response = redshift_data_client.execute_statement(
    WorkgroupName=REDSHIFT_WORKGROUP,
    Database=REDSHIFT_DATABASE,
    Sql=vacuum_sql,
    SecretArn=SECRET_ARN
)

print(f"VACUUM command: {response['Id']}")

job.commit()
print("Dimension load completed successfully")
```

---

### Pattern 2: Fact Table UPSERT (Delete + Insert)

For fact tables with composite keys:

```python
# Configuration
S3_STAGING_PATH = "s3://bucket/staging/fact_aum_revenue/"

# COPY to staging
copy_sql = f"""
TRUNCATE TABLE stg_fact_aum_revenue;

COPY stg_fact_aum_revenue
FROM '{S3_STAGING_PATH}'
IAM_ROLE 'arn:aws:iam::account:role/RedshiftS3Role'
FORMAT AS PARQUET;
"""

# UPSERT logic
upsert_sql = """
BEGIN TRANSACTION;

-- Delete existing records for the same keys
DELETE FROM fact_aum_revenue
USING stg_fact_aum_revenue s
WHERE fact_aum_revenue.fund_key = s.fund_key
  AND fact_aum_revenue.advisor_key = s.advisor_key
  AND fact_aum_revenue.date_key = s.date_key
  AND fact_aum_revenue.version_key = s.version_key;

-- Insert new/updated records
INSERT INTO fact_aum_revenue
SELECT * FROM stg_fact_aum_revenue;

END TRANSACTION;
"""

# Execute
redshift_data_client.execute_statement(
    WorkgroupName=REDSHIFT_WORKGROUP,
    Database=REDSHIFT_DATABASE,
    Sql=copy_sql,
    SecretArn=SECRET_ARN
)

# ... wait for completion ...

redshift_data_client.execute_statement(
    WorkgroupName=REDSHIFT_WORKGROUP,
    Database=REDSHIFT_DATABASE,
    Sql=upsert_sql,
    SecretArN=SECRET_ARN
)
```

---

## 5. Partitioning Strategy {#partitioning}

### S3 Parquet Partitioning

```python
from pyspark.sql.functions import *

# Read and transform
df = spark.read.csv("s3://bucket/raw/", header=True)

# Add partition columns
df_partitioned = df \
    .withColumn("year", year(col("transaction_date"))) \
    .withColumn("month", month(col("transaction_date"))) \
    .withColumn("day", dayofmonth(col("transaction_date")))

# Write with partitioning
df_partitioned.write \
    .mode("append") \
    .partitionBy("year", "month", "day") \
    .parquet("s3://bucket/staging/fact_aum_revenue/")

# Result structure:
# s3://bucket/staging/fact_aum_revenue/
#   year=2025/
#     month=01/
#       day=01/
#         part-00000.parquet
#       day=02/
#         part-00000.parquet
```

### Benefits of Partitioning

1. **Query Performance:** Partition pruning
   ```python
   # Only reads Jan 2025 partitions
   df = spark.read.parquet("s3://bucket/staging/fact/") \
       .filter((col("year") == 2025) & (col("month") == 1))
   ```

2. **Incremental Processing:** Easy to reprocess specific dates
   ```python
   # Reprocess just yesterday
   yesterday_path = f"s3://bucket/staging/fact/year=2025/month=01/day=14/"
   ```

3. **Data Management:** Easy to drop old partitions
   ```python
   # Delete old data
   import boto3
   s3 = boto3.client('s3')
   # Delete year=2023 partition
   ```

---

### Redshift Table Partitioning

Redshift doesn't have true partitioning, but uses **Sort Keys** and **Distribution Keys**:

```sql
-- Fact table with sort key on date
CREATE TABLE fact_aum_revenue (
    fund_key INT,
    advisor_key INT,
    date_key INT,
    version_key INT,
    fee_paying_aum NUMERIC(18,2),
    revenue NUMERIC(18,2)
)
DISTSTYLE KEY
DISTKEY (fund_key)  -- Distribute by fund for JOINs
SORTKEY (date_key, fund_key);  -- Sort by date for time-based queries

-- Dimension with ALL distribution (small table)
CREATE TABLE dim_version (
    version_key INT,
    version_name VARCHAR(50)
)
DISTSTYLE ALL;  -- Copy to all nodes
```

---

## 6. Performance Optimization {#optimization}

### Glue Job Tuning

```python
# Configure Glue job for better performance
spark.conf.set("spark.sql.adaptive.enabled", "true")
spark.conf.set("spark.sql.adaptive.coalescePartitions.enabled", "true")
spark.conf.set("spark.sql.shuffle.partitions", "200")  # Adjust based on data size

# Broadcast small dimensions for joins
from pyspark.sql.functions import broadcast

large_fact_df = spark.read.parquet("s3://bucket/staging/fact/")
small_dim_df = spark.read.parquet("s3://bucket/staging/dim_version/")

# Broadcast join (faster for small dimensions)
joined_df = large_fact_df.join(
    broadcast(small_dim_df),
    "version_key"
)
```

### Caching for Multiple Operations

```python
# Cache dimension if used multiple times
dim_fund_df = spark.read.parquet("s3://bucket/staging/dim_fund/")
dim_fund_df.cache()  # Keep in memory

# Use multiple times
result1 = fact_df.join(dim_fund_df, "fund_key")
result2 = another_fact_df.join(dim_fund_df, "fund_key")

# Don't forget to unpersist when done
dim_fund_df.unpersist()
```

### Repartition Before Write

```python
# Repartition to control output file size
df = spark.read.csv("s3://bucket/raw/")

# Too many small files? Coalesce
df.coalesce(10).write.parquet("s3://bucket/staging/")

# Too few large files? Repartition
df.repartition(100).write.parquet("s3://bucket/staging/")

# Repartition by column for better partitioning
df.repartition("fund_code").write.parquet("s3://bucket/staging/")
```

### Pushdown Predicates to Source

```python
# BAD: Read all data then filter
df = spark.read.parquet("s3://bucket/large_dataset/")
filtered_df = df.filter(col("date") == "2025-01-15")

# GOOD: Filter at read time (partition pruning)
df = spark.read.parquet("s3://bucket/large_dataset/") \
    .filter(col("date") == "2025-01-15")  # Pushed to scan

# BEST: Read only specific partition
df = spark.read.parquet("s3://bucket/large_dataset/year=2025/month=01/day=15/")
```

---

## 7. Complete Glue Job Examples {#complete-jobs}

### Example 1: Full Dimension Load (Dim_Fund with SCD Type 2)

```python
import sys
from awsglue.transforms import *
from awsglue.utils import getResolvedOptions
from pyspark.context import SparkContext
from awsglue.context import GlueContext
from awsglue.job import Job
from pyspark.sql.functions import *
from pyspark.sql.window import Window
from datetime import datetime, timedelta
import boto3

# ============================================================================
# Initialize
# ============================================================================
args = getResolvedOptions(sys.argv, ['JOB_NAME', 'SOURCE_FILE', 'TARGET_TABLE'])
sc = SparkContext()
glueContext = GlueContext(sc)
spark = glueContext.spark_session
job = Job(glueContext)
job.init(args['JOB_NAME'], args)

# Configuration
SOURCE_FILE = args['SOURCE_FILE']  # s3://bucket/input/fund.csv
TARGET_TABLE = args['TARGET_TABLE']  # dim_fund
S3_STAGING = "s3://nyl-invgai-dev-s3-anaplan-bucket/Data/Staging/"
CURRENT_DATE = datetime.now().date()

print(f"Processing: {SOURCE_FILE} -> {TARGET_TABLE}")

# ============================================================================
# Read and Clean Source
# ============================================================================
source_df = spark.read.option("header", "true").csv(SOURCE_FILE)

# Clean column names
for col_name in source_df.columns:
    clean_name = col_name.lower().replace(" ", "_").replace("-", "_")
    source_df = source_df.withColumnRenamed(col_name, clean_name)

# Cast data types
source_df = source_df \
    .withColumn("management_fee", col("management_fee").cast("decimal(5,4)")) \
    .withColumn("expense_ratio", col("expense_ratio").cast("decimal(5,4)"))

print(f"Source records: {source_df.count()}")

# ============================================================================
# Read Current Dimension
# ============================================================================
try:
    current_dim_df = spark.read.parquet(f"{S3_STAGING}{TARGET_TABLE}/")
    max_key = current_dim_df.agg({"fund_key": "max"}).collect()[0][0] or 0
except:
    # First load
    current_dim_df = spark.createDataFrame([], schema=source_df.schema)
    max_key = 0

# ============================================================================
# SCD Type 2 Logic
# ============================================================================

# Get current active records
current_active = current_dim_df.filter(col("is_current") == True)

# Detect changes
comparison = source_df.alias("src").join(
    current_active.alias("curr"),
    (col("src.fund_code") == col("curr.fund_code")) &
    (col("src.share_class") == col("curr.share_class")),
    "left"
).withColumn(
    "change_type",
    when(col("curr.fund_key").isNull(), "INSERT")
    .when(
        (col("src.management_fee") != col("curr.management_fee")) |
        (col("src.fund_name") != col("curr.fund_name")),
        "UPDATE"
    )
    .otherwise("NO_CHANGE")
)

# Process inserts
inserts = comparison.filter(col("change_type") == "INSERT")
window_spec = Window.orderBy(monotonically_increasing_id())
inserts_final = inserts \
    .withColumn("fund_key", row_number().over(window_spec) + max_key) \
    .withColumn("effective_date", lit(CURRENT_DATE)) \
    .withColumn("end_date", lit(None).cast("date")) \
    .withColumn("is_current", lit(True)) \
    .withColumn("version", lit(1))

# Process updates
updates = comparison.filter(col("change_type") == "UPDATE")
max_key_after_inserts = max_key + inserts.count()
updates_final = updates \
    .withColumn("fund_key", row_number().over(window_spec) + max_key_after_inserts) \
    .withColumn("effective_date", lit(CURRENT_DATE)) \
    .withColumn("end_date", lit(None).cast("date")) \
    .withColumn("is_current", lit(True)) \
    .withColumn("version", col("curr.version") + 1)

# Expire old records
changed_keys = updates.select("src.fund_code", "src.share_class").distinct()
expired = current_active.join(
    changed_keys,
    (current_active.fund_code == changed_keys.fund_code) &
    (current_active.share_class == changed_keys.share_class),
    "inner"
).select(current_active["*"]) \
 .withColumn("end_date", lit(CURRENT_DATE - timedelta(days=1))) \
 .withColumn("is_current", lit(False))

# Combine all
select_cols = [
    "fund_key", "fund_code", "fund_name", "fund_family_code",
    "fund_family_name", "share_class", "management_fee",
    "effective_date", "end_date", "is_current", "version"
]

final_df = inserts_final.select(*select_cols) \
    .union(updates_final.select(*select_cols)) \
    .union(expired.select(*select_cols))

# Add unchanged records
unchanged = current_active.join(changed_keys, "fund_code", "left_anti")
final_df = final_df.union(unchanged.select(*select_cols))

print(f"Final dimension: {final_df.count()} records")

# ============================================================================
# Write to S3
# ============================================================================
final_df.write.mode("overwrite").parquet(f"{S3_STAGING}{TARGET_TABLE}/")

# ============================================================================
# Load to Redshift
# ============================================================================
redshift_client = boto3.client('redshift-data', region_name='us-east-1')

# COPY to staging
copy_sql = f"""
TRUNCATE TABLE stg_{TARGET_TABLE};
COPY stg_{TARGET_TABLE} FROM '{S3_STAGING}{TARGET_TABLE}/'
IAM_ROLE 'arn:aws:iam::231139216201:role/application/nyl-invgai-dev-redshift_s3_role'
FORMAT AS PARQUET;
"""

# Execute COPY
resp = redshift_client.execute_statement(
    WorkgroupName='nyl-invgai-wg',
    Database='nyl_anaplan_db',
    Sql=copy_sql,
    SecretArn='arn:aws:secretsmanager:us-east-1:231139216201:secret:redshift/anaplan_batch_user-nWKC1N'
)

print(f"COPY started: {resp['Id']}")

job.commit()
```

---

### Example 2: Fact Load with Dimension Lookup

```python
import sys
from awsglue.transforms import *
from awsglue.utils import getResolvedOptions
from pyspark.context import SparkContext
from awsglue.context import GlueContext
from awsglue.job import Job
from pyspark.sql.functions import *

# Initialize
args = getResolvedOptions(sys.argv, ['JOB_NAME'])
sc = SparkContext()
glueContext = GlueContext(sc)
spark = glueContext.spark_session
job = Job(glueContext)
job.init(args['JOB_NAME'], args)

# Configuration
S3_RAW = "s3://bucket/raw/aum_revenue.csv"
S3_STAGING = "s3://bucket/staging/"
LOAD_DATE = "2025-01-15"

# ============================================================================
# Read Source Fact Data
# ============================================================================
source_df = spark.read.option("header", "true").csv(S3_RAW)

# Clean
for col_name in source_df.columns:
    clean_name = col_name.lower().replace(" ", "_")
    source_df = source_df.withColumnRenamed(col_name, clean_name)

source_df = source_df \
    .withColumn("transaction_date", to_date(col("transaction_date"))) \
    .withColumn("fee_paying_aum", col("fee_paying_aum").cast("decimal(18,2)")) \
    .withColumn("revenue", col("revenue").cast("decimal(18,2)"))

# ============================================================================
# Read Dimensions
# ============================================================================

# Dim_Fund (current records only)
dim_fund = spark.read.parquet(f"{S3_STAGING}dim_fund/") \
    .filter(col("is_current") == True) \
    .select("fund_key", "fund_code", "share_class")

# Dim_Advisor
dim_advisor = spark.read.parquet(f"{S3_STAGING}dim_advisor/") \
    .filter(col("is_current") == True) \
    .select("advisor_key", "advisor_code")

# Dim_Date
dim_date = spark.read.parquet(f"{S3_STAGING}dim_date/") \
    .select("date_key", "full_date")

# Dim_Version
dim_version = spark.read.parquet(f"{S3_STAGING}dim_version/") \
    .select("version_key", "version_code")

# ============================================================================
# Enrich with Dimension Keys
# ============================================================================

fact_df = source_df \
    .join(dim_fund, ["fund_code", "share_class"], "left") \
    .join(dim_advisor, source_df.advisor_code == dim_advisor.advisor_code, "left") \
    .join(dim_date, source_df.transaction_date == dim_date.full_date, "left") \
    .join(dim_version, source_df.version == dim_version.version_code, "left")

# Handle missing dimensions (use -1 for "Unknown")
fact_df = fact_df \
    .withColumn("fund_key", coalesce(col("fund_key"), lit(-1))) \
    .withColumn("advisor_key", coalesce(col("advisor_key"), lit(-1))) \
    .withColumn("version_key", coalesce(col("version_key"), lit(-1)))

# Select final fact columns
fact_final = fact_df.select(
    "fund_key",
    "advisor_key", 
    "date_key",
    "version_key",
    "fee_paying_aum",
    "revenue"
).withColumn("load_timestamp", current_timestamp())

print(f"Fact records: {fact_final.count()}")

# Check for unmapped records
unmapped = fact_final.filter(
    (col("fund_key") == -1) |
    (col("advisor_key") == -1) |
    (col("date_key").isNull())
)

if unmapped.count() > 0:
    print(f"WARNING: {unmapped.count()} records have missing dimension keys")
    unmapped.show(10)

# ============================================================================
# Write to S3 (Partitioned)
# ============================================================================
fact_final \
    .withColumn("year", lit(2025)) \
    .withColumn("month", lit(1)) \
    .withColumn("day", lit(15)) \
    .write \
    .mode("overwrite") \
    .partitionBy("year", "month", "day") \
    .parquet(f"{S3_STAGING}fact_aum_revenue/")

job.commit()
```

---

## 8. Error Handling and Logging {#error-handling}

### Comprehensive Error Handling

```python
import sys
from awsglue.transforms import *
from awsglue.utils import getResolvedOptions
from pyspark.context import SparkContext
from awsglue.context import GlueContext
from awsglue.job import Job
import boto3
from datetime import datetime
import traceback

# Initialize
args = getResolvedOptions(sys.argv, ['JOB_NAME'])
sc = SparkContext()
glueContext = GlueContext(sc)
spark = glueContext.spark_session
job = Job(glueContext)
job.init(args['JOB_NAME'], args)

# DynamoDB for logging
dynamodb = boto3.resource('dynamodb')
log_table = dynamodb.Table('etl_job_logs')

# SNS for alerts
sns = boto3.client('sns')
sns_topic_arn = 'arn:aws:sns:us-east-1:account:etl-alerts'

job_id = args['JOB_NAME'] + '_' + datetime.now().strftime('%Y%m%d_%H%M%S')
start_time = datetime.now()

try:
    # ========================================================================
    # Main ETL Logic
    # ========================================================================
    
    print(f"Starting job: {job_id}")
    
    # Read source
    source_df = spark.read.csv("s3://bucket/input/file.csv", header=True)
    source_count = source_df.count()
    print(f"Source records: {source_count}")
    
    # Validation
    if source_count == 0:
        raise ValueError("Source file is empty")
    
    # Transform
    transformed_df = source_df.withColumn("load_date", current_date())
    
    # Write
    transformed_df.write.mode("overwrite").parquet("s3://bucket/output/")
    output_count = transformed_df.count()
    
    # Verify counts match
    if source_count != output_count:
        raise ValueError(f"Record count mismatch: {source_count} vs {output_count}")
    
    # ========================================================================
    # Success Logging
    # ========================================================================
    
    end_time = datetime.now()
    duration = (end_time - start_time).total_seconds()
    
    log_table.put_item(
        Item={
            'job_id': job_id,
            'job_name': args['JOB_NAME'],
            'status': 'SUCCESS',
            'start_time': start_time.isoformat(),
            'end_time': end_time.isoformat(),
            'duration_seconds': int(duration),
            'records_processed': source_count,
            'records_written': output_count
        }
    )
    
    print(f"Job completed successfully in {duration} seconds")
    
    job.commit()

except Exception as e:
    # ========================================================================
    # Error Logging and Alerting
    # ========================================================================
    
    end_time = datetime.now()
    duration = (end_time - start_time).total_seconds()
    error_msg = str(e)
    error_trace = traceback.format_exc()
    
    print(f"ERROR: {error_msg}")
    print(f"Traceback: {error_trace}")
    
    # Log to DynamoDB
    log_table.put_item(
        Item={
            'job_id': job_id,
            'job_name': args['JOB_NAME'],
            'status': 'FAILED',
            'start_time': start_time.isoformat(),
            'end_time': end_time.isoformat(),
            'duration_seconds': int(duration),
            'error_message': error_msg[:1000],  # DynamoDB limit
            'error_trace': error_trace[:5000]
        }
    )
    
    # Send SNS alert
    sns.publish(
        TopicArn=sns_topic_arn,
        Subject=f'Glue Job Failed: {args["JOB_NAME"]}',
        Message=f"""
Job ID: {job_id}
Job Name: {args['JOB_NAME']}
Status: FAILED
Duration: {duration} seconds
Error: {error_msg}

Full Trace:
{error_trace}
        """
    )
    
    # Re-raise to mark job as failed in Glue console
    raise
```

---

### Data Quality Checks

```python
from pyspark.sql.functions import *

def validate_data_quality(df, table_name):
    """
    Perform data quality checks on DataFrame
    """
    checks = []
    
    # Check 1: No null primary keys
    null_keys = df.filter(col("fund_key").isNull()).count()
    checks.append({
        'table': table_name,
        'check': 'null_primary_keys',
        'result': 'PASS' if null_keys == 0 else 'FAIL',
        'details': f'{null_keys} null keys found'
    })
    
    # Check 2: No negative amounts
    negative_aum = df.filter(col("fee_paying_aum") < 0).count()
    checks.append({
        'table': table_name,
        'check': 'negative_amounts',
        'result': 'PASS' if negative_aum == 0 else 'FAIL',
        'details': f'{negative_aum} negative amounts found'
    })
    
    # Check 3: No duplicates on grain
    total_count = df.count()
    distinct_count = df.select("fund_key", "advisor_key", "date_key").distinct().count()
    checks.append({
        'table': table_name,
        'check': 'duplicate_grain',
        'result': 'PASS' if total_count == distinct_count else 'FAIL',
        'details': f'{total_count - distinct_count} duplicates found'
    })
    
    # Check 4: Date range validity
    min_date = df.agg({"date_key": "min"}).collect()[0][0]
    max_date = df.agg({"date_key": "max"}).collect()[0][0]
    checks.append({
        'table': table_name,
        'check': 'date_range',
        'result': 'INFO',
        'details': f'Date range: {min_date} to {max_date}'
    })
    
    return checks

# Usage
quality_results = validate_data_quality(fact_df, 'fact_aum_revenue')

# Log results
for check in quality_results:
    print(f"{check['table']}.{check['check']}: {check['result']} - {check['details']}")
    
    # Write to quality log table
    log_table.put_item(Item={
        'timestamp': datetime.now().isoformat(),
        'table_name': check['table'],
        'check_name': check['check'],
        'result': check['result'],
        'details': check['details']
    })

# Fail job if critical checks fail
failed_checks = [c for c in quality_results if c['result'] == 'FAIL']
if failed_checks:
    raise ValueError(f"Data quality checks failed: {failed_checks}")
```

---

## 9. Job Orchestration with Step Functions {#orchestration}

### Step Function Definition

```json
{
  "Comment": "Dimensional Data Warehouse ETL Pipeline",
  "StartAt": "Load_Dimensions",
  "States": {
    "Load_Dimensions": {
      "Type": "Parallel",
      "Branches": [
        {
          "StartAt": "Load_Dim_Fund",
          "States": {
            "Load_Dim_Fund": {
              "Type": "Task",
              "Resource": "arn:aws:states:::glue:startJobRun.sync",
              "Parameters": {
                "JobName": "dim_fund_scd2_load",
                "Arguments": {
                  "--SOURCE_FILE": "s3://bucket/input/fund.csv",
                  "--TARGET_TABLE": "dim_fund"
                }
              },
              "End": true
            }
          }
        },
        {
          "StartAt": "Load_Dim_Cost_Center",
          "States": {
            "Load_Dim_Cost_Center": {
              "Type": "Task",
              "Resource": "arn:aws:states:::glue:startJobRun.sync",
              "Parameters": {
                "JobName": "dim_cost_center_scd2_load",
                "Arguments": {
                  "--SOURCE_FILE": "s3://bucket/input/cost_center.csv",
                  "--TARGET_TABLE": "dim_cost_center"
                }
              },
              "End": true
            }
          }
        },
        {
          "StartAt": "Load_Dim_Advisor",
          "States": {
            "Load_Dim_Advisor": {
              "Type": "Task",
              "Resource": "arn:aws:states:::glue:startJobRun.sync",
              "Parameters": {
                "JobName": "dim_advisor_scd2_load",
                "Arguments": {
                  "--SOURCE_FILE": "s3://bucket/input/advisor.csv",
                  "--TARGET_TABLE": "dim_advisor"
                }
              },
              "End": true
            }
          }
        }
      ],
      "Next": "Load_Facts"
    },
    "Load_Facts": {
      "Type": "Parallel",
      "Branches": [
        {
          "StartAt": "Load_Fact_AUM_Revenue",
          "States": {
            "Load_Fact_AUM_Revenue": {
              "Type": "Task",
              "Resource": "arn:aws:states:::glue:startJobRun.sync",
              "Parameters": {
                "JobName": "fact_aum_revenue_load",
                "Arguments": {
                  "--LOAD_DATE.$": "$.load_date"
                }
              },
              "End": true
            }
          }
        },
        {
          "StartAt": "Load_Fact_Expense",
          "States": {
            "Load_Fact_Expense": {
              "Type": "Task",
              "Resource": "arn:aws:states:::glue:startJobRun.sync",
              "Parameters": {
                "JobName": "fact_expense_load",
                "Arguments": {
                  "--LOAD_DATE.$": "$.load_date"
                }
              },
              "End": true
            }
          }
        }
      ],
      "Next": "Redshift_Post_Processing"
    },
    "Redshift_Post_Processing": {
      "Type": "Task",
      "Resource": "arn:aws:states:::glue:startJobRun.sync",
      "Parameters": {
        "JobName": "redshift_vacuum_analyze"
      },
      "End": true
    }
  }
}
```

---

## Summary: Recommended Glue + Spark Approach

### For Your Investment Management Data:

1. **Dimension Processing (SCD Type 2)**
   - Use Glue PySpark for SCD logic
   - Write to S3 as Parquet
   - COPY to Redshift staging
   - Simple MERGE in Redshift

2. **Fact Processing**
   - Read dimensions from S3/Redshift
   - Enrich facts with dimension keys
   - Handle missing dimensions (-1 for Unknown)
   - Partition by date in S3
   - DELETE + INSERT in Redshift

3. **Performance**
   - Partition S3 data by year/month/day
   - Use broadcast joins for small dimensions
   - Cache frequently-used dimensions
   - Optimize Glue DPU allocation

4. **Error Handling**
   - Log to DynamoDB
   - Alert via SNS
   - Data quality checks before load
   - Comprehensive error messages

---

**Next Steps:**
1. Set up Glue job templates
2. Create DynamoDB logging tables
3. Configure SNS alerts
4. Test SCD Type 2 with sample data
5. Implement orchestration with Step Functions

---

**Document Version:** 1.0  
**Last Updated:** January 2026  
**Project:** Investment Management Data Warehouse - Glue Implementation